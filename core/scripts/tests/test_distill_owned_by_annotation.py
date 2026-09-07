""" scope addendum: `owned_by` ownership annotation on read-cap distill rows.

`--distill-candidates` emitted `trigger` and `recommended_action` and NOTHING
about ownership, so a reader of tree-debt output could not tell "nobody has
looked at this" from "g-115-8284 owns it, pending". The disposition record
already existed — it is the per-node goal — and what had never been run is the
JOIN. Twice the reader re-derived it instead and filed a fresh census: eleven
nodes on 2026-07-30 and eleven again on 2026-08-30, 31 days apart, same
population, two full investigations (sig-229).

These tests pin the three correctness constraints that make the join more than
a substring scan, each of which was measured rather than assumed:

* ARCHIVE as well as LIVE (guard-1555) — a completed aspiration is archived and
  its goals vanish from the live store, so a live-only join reports a false
  `owned_by: null`. Measured 2026-09-07: 3,097 live vs 2,480 archived goals.
* DESCRIPTION as well as TITLE (guard-2228) — an owner names the SYMPTOM in its
  title and the artifact only in its description. Measured on the real corpus:
  title-only resolved 3 of 6 stems, title+description resolved 6 of 6.
* TITLE OUTRANKS DESCRIPTION — the counterweight to the above. A CENSUS goal
  that merely ENUMERATES node names would otherwise claim ownership of every
  node it counted; g-115-4077's own addendum lists seven stems, and an unranked
  first-match scan named that goal as owner of nodes it had only tallied.

Plus the two invariants that keep it safe: ANNOTATE-NEVER-SUPPRESS, and
FAIL-OPEN (candidate production must never depend on the aspiration store).
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tree.py"
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("tree_engine_g4077", MODULE_PATH)
tree_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree_engine)

import aspirations as asp_mod  # noqa: E402  (sibling on the same sys.path)


def _goal(gid, title, description="", status="pending"):
    return {"id": gid, "title": title, "description": description,
            "status": status}


class OwnedByAnnotationTest(unittest.TestCase):
    """The join itself: what it finds, what it prefers, what it refuses to do."""

    def _store(self, live_goals, archive_goals=()):
        """Point the join at temp live+archive stores. Returns nothing; patches."""
        def _write(goals):
            fd, path = tempfile.mkstemp(suffix=".jsonl")
            with os.fdopen(fd, "w", encoding="utf-8") as h:
                h.write(json.dumps({"id": "asp-test", "goals": list(goals)}) + "\n")
            self.addCleanup(lambda p=path: os.path.exists(p) and os.remove(p))
            return Path(path)

        live_path, arch_path = _write(live_goals), _write(archive_goals)
        orig_live, orig_arch = asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH
        asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH = live_path, arch_path

        def _restore():
            asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH = orig_live, orig_arch
        self.addCleanup(_restore)

    # ── recall: the two axes that find owners at all ─────────────────────

    def test_description_match_found_when_no_title_names_the_node(self):
        # guard-2228: the owner names the SYMPTOM in its title and the node only
        # in its description. A title-only probe returns empty for an owner
        # sitting right there, and that empty reads as "unowned, file it".
        self._store([_goal("g-1-1", "Read cap keeps blocking the sweep",
                           "the offender is pool-operator, 74k chars")])
        self.assertEqual(
            tree_engine._distill_owner_index(["pool-operator"])["pool-operator"],
            "g-1-1")

    def test_hyphen_and_space_spellings_both_match(self):
        # The measured  miss: an owner spelling the stem with a SPACE
        # is invisible to a hyphenated match, which turned a real owner into a
        # "no owner" probe artifact.
        self._store([_goal("g-2-1", "directive lane series shards exceed the cap")])
        got = tree_engine._distill_owner_index(["directive-lane-series"])
        self.assertEqual(got["directive-lane-series"], "g-2-1")

    def test_archive_is_scanned_not_only_the_live_store(self):
        # guard-1555: a completed aspiration is archived and every goal in it
        # disappears from the live store. A live-only join calls that null.
        self._store(live_goals=[_goal("g-3-1", "unrelated")],
                    archive_goals=[_goal("g-3-9", "fix widget-node read cap",
                                         status="completed")])
        self.assertEqual(
            tree_engine._distill_owner_index(["widget-node"])["widget-node"],
            "g-3-9")

    # ── precision: the ranking that keeps recall from lying ──────────────

    def test_title_match_outranks_description_match(self):
        # THE CENSUS TRAP. A goal that merely ENUMERATES node names would
        # otherwise own every node it counted. Measured on the real corpus:
        # unranked first-match named  for checker-input-assumption-
        # defects where an independent census had identified .
        self._store([
            _goal("g-4-CENSUS", "Census of over-cap nodes",
                  "counted: alpha-node, beta-node, gamma-node"),
            _goal("g-4-OWNER", "beta-node exceeds the read cap",
                  "the actual fix goal"),
        ])
        self.assertEqual(
            tree_engine._distill_owner_index(["beta-node"])["beta-node"],
            "g-4-OWNER",
            "a title hit is a claim ABOUT the node; a description hit may be a "
            "claim about a LIST that contains it")

    def test_open_goal_outranks_terminal_goal_at_equal_match_strength(self):
        # "Is someone on this?" and "has anyone looked?" are different reader
        # questions; the open goal answers the more useful one.
        self._store([
            _goal("g-5-DONE", "delta-node read cap", status="completed"),
            _goal("g-5-OPEN", "delta-node read cap", status="pending"),
        ])
        self.assertEqual(
            tree_engine._distill_owner_index(["delta-node"])["delta-node"],
            "g-5-OPEN")

    def test_unmatched_stem_reports_none_rather_than_a_nearest_guess(self):
        self._store([_goal("g-6-1", "something else entirely")])
        self.assertIsNone(
            tree_engine._distill_owner_index(["no-such-node"])["no-such-node"])

    # ── safety: the two invariants ───────────────────────────────────────

    def test_unreadable_store_fails_open_to_none_and_never_raises(self):
        # Candidate production must never depend on the aspiration store being
        # readable. A missing owner is a weaker annotation; an exception is a
        # broken producer.
        orig_live, orig_arch = asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH
        asp_mod.LIVE_PATH = Path("/nonexistent/live.jsonl")
        asp_mod.ARCHIVE_PATH = Path("/nonexistent/archive.jsonl")
        self.addCleanup(lambda: (setattr(asp_mod, "LIVE_PATH", orig_live),
                                 setattr(asp_mod, "ARCHIVE_PATH", orig_arch)))
        got = tree_engine._distill_owner_index(["any-node"])
        self.assertEqual(got, {"any-node": None})

    def test_empty_stems_short_circuits_without_touching_the_store(self):
        # The common path: no read-cap candidate fired, so no scan is owed.
        orig_live = asp_mod.LIVE_PATH
        asp_mod.LIVE_PATH = Path("/nonexistent/should-not-be-read.jsonl")
        self.addCleanup(lambda: setattr(asp_mod, "LIVE_PATH", orig_live))
        self.assertEqual(tree_engine._distill_owner_index([]), {})


class AnnotateNeverSuppressTest(unittest.TestCase):
    """The HARD CONSTRAINT: `owned_by` adds a field and removes no row.

    `distill_exempt` deliberately does not suppress the read-cap arm (pinned by
    test_distill_exemption_does_NOT_suppress_the_readcap_arm). An `owned_by`
    that filtered would hide genuinely over-cap nodes behind stale goals — the
    opposite of what the annotation exists to do.
    """

    def _write_tmp(self, text):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _oversized_tree(self):
        body = "## Architecture overview\n" + ("plain payload text line\n" * 4000)
        return {"nodes": {"n": {"file": self._write_tmp(body),
                                "retrieval_count": 0, "utility_ratio": 0.0,
                                "times_helpful": 0, "times_noise": 0,
                                "children": []}}}

    def _patch_join(self, fn):
        orig = tree_engine._distill_owner_index
        tree_engine._distill_owner_index = fn
        self.addCleanup(lambda: setattr(tree_engine, "_distill_owner_index", orig))

    def test_rows_are_identical_whether_or_not_an_owner_resolves(self):
        tree = self._oversized_tree()

        self._patch_join(lambda stems: {s: None for s in stems})
        unowned = tree_engine.get_distill_candidates(tree)

        self._patch_join(lambda stems: {s: "g-999-99" for s in stems})
        owned = tree_engine.get_distill_candidates(tree)

        self.assertEqual([c["key"] for c in unowned], [c["key"] for c in owned],
                         "annotation must not add or drop rows")
        self.assertTrue(unowned, "fixture must actually produce a read-cap row")
        self.assertIsNone(unowned[0]["owned_by"])
        self.assertEqual(owned[0]["owned_by"], "g-999-99")

    def test_a_raising_join_does_not_break_candidate_production(self):
        # Fail-open at the CALL SITE too, not only inside the helper.
        tree = self._oversized_tree()
        baseline = tree_engine.get_distill_candidates(tree)

        def _boom(stems):
            raise RuntimeError("aspiration store exploded")

        self._patch_join(_boom)
        try:
            after = tree_engine.get_distill_candidates(tree)
        except Exception as exc:  # pragma: no cover - the assertion IS the point
            self.fail("a failing ownership join must never break the producer: %r" % exc)
        self.assertEqual([c["key"] for c in baseline], [c["key"] for c in after])

    def test_owned_by_key_is_present_on_every_emitted_row(self):
        # Schema stability: `trigger` and `recommended_action` are emitted
        # unconditionally and this is the same shape, so consumers never have
        # to distinguish "absent key" from "no owner".
        tree = self._oversized_tree()
        for cand in tree_engine.get_distill_candidates(tree):
            self.assertIn("owned_by", cand)


if __name__ == "__main__":
    unittest.main()
