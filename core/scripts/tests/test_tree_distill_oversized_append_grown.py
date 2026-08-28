"""test_tree_distill_oversized_append_grown.py -  regression test.

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

    def test_token_estimate_uses_calibrated_ratio_not_chars_over_4(self):
        """The estimate divides by CHARS_PER_TOKEN, not the old 4.

        This test previously asserted `tok == 1000` for 4000 chars, pinning
        chars//4 — i.e. it pinned the DEFECT. chars//4 underestimates dense
        technical markdown by ~1.7x, always in the flattering direction, which
        scored 5 over-cap tree nodes as "HEALTHY" and inverted crit3's proactive
        headroom (its 80% trigger sat at ~135% of true cap). Updated with the
        fix (g-115-3553) per guard-1696: changing behavior a test pins obliges
        updating the test, not deleting it.
        """
        lc, tok, refs = tree_engine._analyze_node_body("x" * 4000)
        self.assertEqual(tok, int(4000 / tree_engine.CHARS_PER_TOKEN))
        # Directional guarantee that matters more than the exact arithmetic:
        # the estimate must be HIGHER than chars//4, because erring high is the
        # fail-safe direction (rb-2077 — a node past the Read cap returns a
        # truncated read, which does not satisfy the read-before-edit gate).
        self.assertGreater(tok, 4000 // 4)

    def test_token_estimate_matches_measured_token_count(self):
        """Calibration pin: the ratio must reproduce a REAL measured count.

        Ground truth is the exact token count reported by the Read tool's
        truncation notice for product-world-model.md on 2026-07-27, when the
        file was 67730 bytes: 29270 tokens. Asserting on a synthetic string can
        only re-derive the arithmetic; only a measured pair can catch the ratio
        drifting away from the content class it was calibrated on.

        Tolerance is 5% — tight enough to fail if the ratio moves back toward 4
        (which lands 42% low) and loose enough to survive tokenizer revisions.
        """
        chars, measured = 67730, 29270
        _, tok, _ = tree_engine._analyze_node_body("x" * chars)
        err = abs(tok - measured) / measured
        self.assertLess(
            err, 0.05,
            f"estimate {tok} is {100 * err:.1f}% off the measured {measured} for "
            f"{chars} chars of dense technical markdown; CHARS_PER_TOKEN="
            f"{tree_engine.CHARS_PER_TOKEN} may have drifted off its calibration",
        )

    def test_goal_id_stamped_headings_count_as_append_sections(self):
        """A heading stamped with a GOAL ID is an append-grown section marker.

        Keyed on the STAMP, not the word "refresh": measured 2026-07-30, this
        tree's three most append-grown nodes name sections by goal id
        (directive-lane-series 19, arc-agi-3 27,
        env-agnostic-exploration-primitives 23) and therefore scored 0-2 against
        the >=3 bar — invisible to the detector built to catch them. (g-115-4057)
        """
        text = (
            "## g-315-330 — dominance-only quarantine SHIPPED\nbody\n"
            "## g-315-329 — commit-quarantine CORRECTED\nbody\n"
            "## g-315-327 — visits-poisoning WORKS\nbody\n"
        )
        _, _, refs = tree_engine._analyze_node_body(text)
        self.assertEqual(refs, 3)

    def test_iso_date_stamped_headings_count(self):
        _, _, refs = tree_engine._analyze_node_body(
            "## 2026-07-30 sweep\nbody\n## 2026-07-23 sweep\nbody\n")
        self.assertEqual(refs, 2)

    def test_stamps_in_body_text_do_not_count(self):
        """Precision guard for the broadened regex: the heading gate still
        applies first, so a goal id or date in PROSE must not register. Without
        this, the broadened signature would fire on any node that merely cites
        goals — and crit3 would become the false-positive pool crit1 already is.
        """
        text = (
            "we shipped g-315-330 here and again on 2026-07-30\n"
            "another line mentioning g-115-4057\n"
        )
        _, _, refs = tree_engine._analyze_node_body(text)
        self.assertEqual(refs, 0)

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


class TestPresentButNoneUtilityRatio(unittest.TestCase):
    """: `.get('utility_ratio', 0.0)` returns the default only when the
    key is ABSENT. Five live nodes carried it PRESENT-but-null, and `None < 0.3`
    raised TypeError — so --distill-candidates returned NOTHING and every debt
    figure derived from it died too. A whole maintenance operation was inert and
    its only symptom was one error line from a scanner nobody reads on success.
    """

    @staticmethod
    def _node(**over):
        n = {
            "retrieval_count": 9,
            "times_helpful": 0,
            "times_noise": 3,
            "last_retrieved": date.today().isoformat(),
            "file": "",
        }
        n.update(over)
        return n

    def _keys(self, tree):
        return {c["key"] for c in tree_engine.get_distill_candidates(tree)}

    def test_none_utility_ratio_does_not_raise(self):
        tree = {"nodes": {"nully": self._node(utility_ratio=None)}}
        self._keys(tree)  # must not raise TypeError

    def test_none_utility_ratio_is_not_treated_as_low_utility(self):
        """None means NEVER MEASURED, which is not the claim "measured, and
        zero". Coercing it to 0.0 would make the comparison safe while feeding
        these nodes into exactly the crit1/crit2 false-positive pool that
        g-115-137 / g-115-2317 exist to drain — a silent regression that no
        crash would announce."""
        tree = {"nodes": {"nully": self._node(utility_ratio=None)}}
        self.assertNotIn("nully", self._keys(tree))

    def test_measured_zero_utility_IS_still_flagged(self):
        """Positive control. Without this, the test above would pass just as well
        against a change that suppressed every utility candidate outright."""
        tree = {"nodes": {"measured": self._node(utility_ratio=0.0)}}
        self.assertIn("measured", self._keys(tree))


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
        # : maintain_exempt:["distill"] suppresses the UTILITY triggers
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
        # : votes + fresh last_retrieved keep crit1 otherwise-live so
        # the exemption is provably what clears it.
        tree = {"nodes": {"niche-node": self._node(
            file="", retrieval_count=10, utility_ratio=0.1,
            times_helpful=3, times_noise=0,
            last_retrieved=date.today().isoformat(),
            maintain_exempt=["distill"])}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("niche-node", cands)

    def test_oversized_without_append_grown_IS_flagged_with_regroup(self):
        """: the read-cap arm must fire INDEPENDENTLY of append-grown-ness.

        ANDing the two made the read-cap question unanswerable for a node that is
        oversized without being append-grown. Measured 2026-07-30 on live data:
        framework-guardrails-and-gates (117% of cap, 1 append section) and
        product-world-model (113%, 1) were permanently invisible at ANY size.

        The action must be `regroup`, NOT `distill` — the rb-2085 procedure rolls
        up dated sections, and these nodes have none to roll up. Handing them
        `distill` is the false-positive class the refresh_min gate guards against,
        which is why the fix branches the ACTION rather than loosening the gate.
        """
        body = "## Architecture overview\n" + ("plain payload text line\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"big-flat-node": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertIn("big-flat-node", cands)
        c = cands["big-flat-node"]
        self.assertEqual(c["trigger"], "oversized_not_append_grown")
        self.assertEqual(c["recommended_action"], "regroup")
        self.assertLess(c["refresh_sections"], 3)

    def test_append_grown_arm_still_recommends_distill(self):
        """Positive control for the split: the ORIGINAL arm must be untouched.
        Without this, the test above would pass equally against a change that
        routed every read-cap candidate to regroup and broke rb-2085 routing."""
        body = ("## g-315-330 ship\n## g-315-329 fix\n## g-315-327 probe\n"
                + ("plain payload text line\n" * 4000))
        path = self._write_tmp(body)
        tree = {"nodes": {"sweep-node-2": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}}
        c = {x["key"]: x for x in tree_engine.get_distill_candidates(tree)}["sweep-node-2"]
        self.assertEqual(c["trigger"], "oversized_append_grown")
        self.assertEqual(c["recommended_action"], "distill")

    def test_redistribute_exemption_suppresses_the_readcap_arm(self):
        """An intentionally-wide hub opts out of the regroup FAMILY, so a
        `redistribute` exemption must suppress this arm — the flag speaks to the
        action actually being recommended."""
        body = "## Architecture overview\n" + ("plain payload text line\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"wide-hub": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            maintain_exempt=["redistribute"])}}
        self.assertNotIn("wide-hub",
                         {c["key"] for c in tree_engine.get_distill_candidates(tree)})

    def test_distill_exemption_does_NOT_suppress_the_readcap_arm(self):
        """Converse of the above, and the reason the two flags are not
        interchangeable: a `distill` exemption is a judgment about the DISTILL
        action. It must not suppress a `regroup` recommendation, or a node could
        opt out of one action and silently escape a different one."""
        body = "## Architecture overview\n" + ("plain payload text line\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"exempt-but-huge": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            maintain_exempt=["distill"])}}
        c = {x["key"]: x for x in tree_engine.get_distill_candidates(tree)}
        self.assertIn("exempt-but-huge", c)
        self.assertEqual(c["exempt-but-huge"]["recommended_action"], "regroup")

    def test_both_readcap_triggers_share_the_top_sort_tier(self):
        """An over-cap node is unreadable however it grew, so ranking the
        non-append-grown arm behind low-utility leaves would re-create the
        invisibility the split just fixed — it would fire and then never surface
        within a per-invocation cap."""
        flat = self._write_tmp("## Overview\n" + ("payload line\n" * 4000))
        grown = self._write_tmp("## g-315-330 a\n## g-315-329 b\n## g-315-327 c\n"
                                + ("payload line\n" * 4000))
        small_low_util = self._write_tmp("## Notes\n" + ("payload line\n" * 60))
        tree = {"nodes": {
            "flat-huge": self._node(file=flat, retrieval_count=50, utility_ratio=0.95),
            "grown-huge": self._node(file=grown, retrieval_count=50, utility_ratio=0.95),
            "low-util": self._node(file=small_low_util, retrieval_count=9,
                                   utility_ratio=0.0, times_helpful=0, times_noise=3,
                                   last_retrieved=date.today().isoformat()),
        }}
        order = [c["trigger"] for c in tree_engine.get_distill_candidates(tree)]
        readcap = {"oversized_append_grown", "oversized_not_append_grown"}
        self.assertTrue(all(t in readcap for t in order[:2]),
                        f"read-cap tier must lead; got {order}")

    def test_oversized_without_append_grown_no_longer_escapes(self):
        """SUPERSEDES test_oversized_without_refresh_not_crit3 ().

        That test asserted `assertNotIn` — an oversized, non-append-grown node was
        expected to be NO candidate at all. That was the contract, and the contract
        was the bug: it made the read-cap question unanswerable for such a node at
        any size, which is why framework-guardrails-and-gates sat at 117% of the
        Read cap while being scored the same as a healthy node.

        Kept as an explicit supersession record rather than deleted, because the
        assertion did not become WRONG by accident — it was rewritten deliberately
        when the trigger/action split landed, and a future reader who finds only
        the new tests cannot tell that the old contract was retired on purpose.

        (This fixture also carries the g-115-4057 correction: its headings were
        once "## Section heading 2026-06-21", written when the append-grown
        signature was the WORD "refresh" so a merely-dated heading was a valid
        negative. The signature is now the STAMP, so the headings below carry
        neither a date nor a goal id.)
        """
        body = "## Architecture overview\n" * 4 + ("plain payload text line\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"big-non-sweep": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95)}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertIn("big-non-sweep", cands)
        self.assertEqual(cands["big-non-sweep"]["trigger"],
                         "oversized_not_append_grown")
        self.assertEqual(cands["big-non-sweep"]["recommended_action"], "regroup")

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
            # crit1, file="" (size N/A). : crit1 now needs
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

    def test_oversized_interior_node_is_crit3_candidate(self):
        #  / rb-4648: an INTERIOR node (with children) that is
        # oversized + append-grown MUST now surface via crit3. Before the fix
        # this loop skipped interior nodes entirely (an early `continue`), so an
        # oversized index hub too big to Read escaped ALL THREE distill criteria
        # AND the retired- redistribute body-trigger AND decompose
        # (leaf-count) — every maintenance detector was blind to it. crit3 is
        # structural (read-cap protection), so an oversized interior hub is
        # exactly its target.
        body = "## Refresh 2026-06-21\n" * 4 + ("payload line of sweep text\n" * 4000)
        path = self._write_tmp(body)
        tree = {"nodes": {"oversized-hub": self._node(
            file=path, retrieval_count=50, utility_ratio=0.95,
            times_helpful=10, times_noise=0, children=["child-a", "child-b"])}}
        cands = {c["key"]: c for c in tree_engine.get_distill_candidates(tree)}
        self.assertIn("oversized-hub", cands)  # crit3 fires on interior nodes now
        self.assertEqual(cands["oversized-hub"]["trigger"], "oversized_append_grown")

    def test_small_interior_node_still_skipped(self):
        # A non-oversized interior node is NOT a candidate — crit3 does not trip
        # and crit1/crit2 are leaf-only. include_skipped attributes it to
        # has_children (refined meaning: interior, crit3-eligible, crit3 did not
        # trip).
        tree = {"nodes": {"small-hub": self._node(
            file="", retrieval_count=50, utility_ratio=0.95,
            times_helpful=10, children=["child-a"])}}
        result = tree_engine.get_distill_candidates(tree, include_skipped=True)
        cand_keys = {c["key"] for c in result["candidates"]}
        self.assertNotIn("small-hub", cand_keys)
        skips = {s["node_key"]: s["skip_reason"] for s in result["skipped"]}
        self.assertEqual(skips.get("small-hub"), "has_children")

    def test_interior_node_low_utility_does_not_trip_crit1(self):
        # crit1/crit2 are leaf-only: a low-utility interior node that WOULD trip
        # crit1 as a leaf (rc>=min, >=min_votes feedback, fresh signal, ur<0.3)
        # must NOT become a candidate — only crit3 applies to interior nodes
        # (, symmetric to the distill_exempt suppression).
        tree = {"nodes": {"low-util-hub": self._node(
            file="", retrieval_count=10, utility_ratio=0.1,
            times_helpful=3, times_noise=0,
            last_retrieved=date.today().isoformat(),
            children=["child-a"])}}
        cands = {c["key"] for c in tree_engine.get_distill_candidates(tree)}
        self.assertNotIn("low-util-hub", cands)


class TestReadCapArmRouting(unittest.TestCase):
    """ — step 2.a0 of tree/SKILL.md routes the `regroup` arm.

    The arm holds `oversized_not_append_grown` candidates: too big to Read, too
    few dated sections for the rb-2085 rollup. It used to route them to REGROUP
    (SKILL.md §1.5), which fires on `child_count > K_max`. tree.yaml raised K_max
    4 -> 40 by user directive, retiring count-based regroup pressure — so the arm
    was structurally unable to fire, and `--redistribute-candidates` returns []
    tree-wide while the arm stays populated. Re-pointed at split-overcap's PATH
    FORK, which can actually act on an over-cap node.

    The threshold is DERIVED, not chosen, and that is what these tests pin.
    """

    # tree.py READ-SITE CAVEAT (, rb-5894 / guard-2006): the 25000-token
    # cap times 2.43 — the HIGH end of the measured 2.31-2.43 chars/token band. At
    # or above this many chars a node is over cap at ANY ratio in that band, so it
    # is the only floor a DESTRUCTIVE-shaped consumer may act on.
    BIAS_INDEPENDENT_CHARS = 60750

    def _skill_src(self):
        skill = CORE_SCRIPTS.parent.parent / ".claude" / "skills" / "tree" / "SKILL.md"
        return skill.read_text(encoding="utf-8")

    def test_split_gate_threshold_stays_derived_from_chars_per_token(self):
        # CHARS_PER_TOKEN has already moved once (4 -> 2.3, after the old value was
        # found to underestimate by ~1.7x). If it moves again the SKILL.md literal
        # goes silently wrong in the DESTRUCTIVE direction — under-cap nodes get
        # routed into structural surgery. Fail loudly instead.
        expected = int(self.BIAS_INDEPENDENT_CHARS / tree_engine.CHARS_PER_TOKEN)
        # assertTrue, not assertIn: assertIn dumps the whole 57KB haystack into the
        # failure output, burying the one line that says what to fix.
        self.assertTrue(
            ("est_tokens >= %d" % expected) in self._skill_src(),
            "step 2.a0's split gate must read 'est_tokens >= %d' (= %d chars / "
            "CHARS_PER_TOKEN=%s); CHARS_PER_TOKEN changed without re-deriving the "
            "SKILL.md literal"
            % (expected, self.BIAS_INDEPENDENT_CHARS, tree_engine.CHARS_PER_TOKEN),
        )

    def test_split_gate_is_stricter_than_the_arms_own_trigger(self):
        # The arm fires at 0.8 * cap (proactive, correct for a non-destructive
        # rollup). The split gate must sit STRICTLY above it, or the routing
        # inherits a trigger designed to fire early and applies it to surgery.
        pruning = tree_engine._merged_config().get("pruning", {})
        arm_trigger = (pruning.get("distill_token_ratio", 0.8)
                       * pruning.get("distill_token_cap", 25000))
        gate = int(self.BIAS_INDEPENDENT_CHARS / tree_engine.CHARS_PER_TOKEN)
        self.assertGreater(
            gate, arm_trigger,
            "split gate (%d) must exceed the arm's proactive trigger (%s)"
            % (gate, arm_trigger),
        )

    def test_arm_no_longer_routes_to_the_dormant_regroup_section(self):
        src = self._skill_src()
        start = src.find("a0. ACTION ROUTING")
        self.assertNotEqual(start, -1, "step 2.a0 is missing")
        block = src[start:src.find("\n   a. ", start)]
        self.assertIn("SPLIT-OVERCAP PATH FORK", block,
                      "2.a0 must route the read-cap arm at split-overcap's fork")
        # : anchored on the two facts that must stay TRUE, not on one
        # phrasing of them. This assertion used to require the literal
        # "DO NOT route these to REGROUP"; the SKILL.md was reworded to
        # "do NOT route to REGROUP (§1.5) — that arm is DORMANT BY DESIGN and
        # cannot fire on them", which satisfies the assertion's own stated
        # intent while failing its literal. That is a red carrying no defect —
        # the exact churn the goal was filed against.
        #
        # guard-2723's general form is "grep for what stops being TRUE, not for
        # what stops being PRINTED". Its letter (anchor on a branch CONDITION)
        # does not transfer: this block is prose routing an LLM, so there is no
        # condition to anchor on and the prose IS the defence. Its spirit does,
        # and these two tokens are what any correct phrasing must carry:
        #   REGROUP  — the arm being spoken about at all
        #   DORMANT  — the REASON it must not be routed to
        # Deleting the dormancy explanation, or re-pointing the arm at §1.5 as a
        # live route, removes "DORMANT" and goes red — which is the regression
        # this test exists to catch. Rewording the sentence does not.
        self.assertIn("REGROUP", block,
                      "2.a0 must name the REGROUP arm it is steering away from")
        self.assertIn("DORMANT", block,
                      "2.a0 must say the REGROUP arm is DORMANT — without the "
                      "reason, the next reader re-points it back at §1.5")
        self.assertNotIn("handle the node under REGROUP", block,
                         "the old dormant routing is still present")

    def test_fork_is_not_collapsed_to_an_unconditional_split(self):
        # split-overcap step 0: boundaries are INPUT, never inferred. A node with
        # no natural partition key is a DISTILL. Two of the four live arm members
        # are leaves, so the fork's second branch is load-bearing, not decorative.
        src = self._skill_src()
        start = src.find("a0. ACTION ROUTING")
        block = src[start:src.find("\n   a. ", start)]
        self.assertIn("BOUNDARIES ARE INPUT, NEVER INFERRED", block)
        self.assertIn("that is a DISTILL, not a split", block,
                      "the fork's no-partition-key branch must survive")


if __name__ == "__main__":
    unittest.main()
