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
import re
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

    def test_rank_records_title_vs_description_and_open_vs_terminal(self):
        # : the join always knew HOW STRONG each hit was and threw it
        # away, so a consumer could not tell an owner from a sweep that merely
        # mentions the node. `rank` is (matched_in_title, owner_is_open).
        self._store([
            _goal("g-6-SWEEP", "Recurring freshness sweep",
                  "covers gamma-node among others"),
            _goal("g-6-DONE", "epsilon-node read cap", status="completed"),
        ])
        got = tree_engine._distill_owner_index(
            ["gamma-node", "epsilon-node", "no-such-node"])
        self.assertEqual(got.rank["gamma-node"], (False, True),
                         "description-only hit on an open sweep: not a claim about the node")
        self.assertEqual(got.rank["epsilon-node"], (True, False),
                         "title hit on a terminal goal: names the node, nobody is on it")
        self.assertNotIn("no-such-node", got.rank)
        self.assertEqual(
            got, {"gamma-node": "g-6-SWEEP", "epsilon-node": "g-6-DONE",
                  "no-such-node": None},
            "the mapping itself must stay exactly what every caller always got")

    def test_strength_reaches_the_emitted_rows_through_the_real_join(self):
        # : the seam the other tests straddle. The rank test pins the
        # join and the patched-join test pins the call site; this one runs the
        # real join into the real row, so a rename on either side fails here.
        self._store([
            _goal("g-7-OWN", "titled-node exceeds the read cap"),
            _goal("g-7-SWEEP", "Recurring freshness sweep",
                  "covers swept-node among others"),
        ])

        def _node():
            fd, path = tempfile.mkstemp(suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as h:
                h.write("## Architecture overview\n" + ("plain payload text line\n" * 4000))
            self.addCleanup(lambda p=path: os.path.exists(p) and os.remove(p))
            return {"file": path, "retrieval_count": 0, "utility_ratio": 0.0,
                    "times_helpful": 0, "times_noise": 0, "children": []}

        rows = {c["key"]: c for c in tree_engine.get_distill_candidates(
            {"nodes": {"titled-node": _node(), "swept-node": _node(),
                       "orphan-node": _node()}})}
        self.assertEqual(sorted(rows), ["orphan-node", "swept-node", "titled-node"],
                         "fixture must produce a read-cap row per node")
        self.assertEqual(
            (rows["titled-node"]["owned_by"], rows["titled-node"]["owner_match"],
             rows["titled-node"]["owner_open"]), ("g-7-OWN", "title", True))
        self.assertEqual(
            (rows["swept-node"]["owned_by"], rows["swept-node"]["owner_match"],
             rows["swept-node"]["owner_open"]), ("g-7-SWEEP", "description", True),
            "a sweep that only mentions the node must read as description-only")
        self.assertEqual(
            (rows["orphan-node"]["owned_by"], rows["orphan-node"]["owner_match"],
             rows["orphan-node"]["owner_open"]), (None, None, None))

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

    def test_strength_fields_follow_the_rank_the_join_reports(self):
        # : the call site copies the join's rank onto the row and
        # never upgrades a weak hit. A weak owner still annotates; strength
        # decides whether the reader may skip, not whether the row carries it.
        tree = self._oversized_tree()

        def _ranked(rank):
            def _join(stems):
                owners = tree_engine._OwnerMap({s: "g-999-99" for s in stems})
                owners.rank.update({s: rank for s in stems})
                return owners
            return _join

        self._patch_join(_ranked((True, True)))
        strong = tree_engine.get_distill_candidates(tree)[0]
        self.assertEqual((strong["owner_match"], strong["owner_open"]),
                         ("title", True))

        self._patch_join(_ranked((False, False)))
        weak = tree_engine.get_distill_candidates(tree)[0]
        self.assertEqual((weak["owner_match"], weak["owner_open"]),
                         ("description", False))
        self.assertEqual(weak["owned_by"], "g-999-99")

    def test_a_join_that_reports_no_rank_leaves_strength_unknown(self):
        # A plain mapping (every join the older tests patch in, and the fail-open
        # return) carries no rank. `null` beside a non-null owned_by is the
        # documented "strength unknown: do the lookup", never a silent "strong".
        tree = self._oversized_tree()
        self._patch_join(lambda stems: {s: "g-999-99" for s in stems})
        row = tree_engine.get_distill_candidates(tree)[0]
        self.assertEqual(row["owned_by"], "g-999-99")
        self.assertIsNone(row["owner_match"])
        self.assertIsNone(row["owner_open"])

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
        self.assertTrue(after, "fixture must actually produce a read-cap row")
        for cand in after:
            # A failed join must not leave a half-written strength behind.
            self.assertIsNone(cand["owner_match"])
            self.assertIsNone(cand["owner_open"])

    def test_owned_by_key_is_present_on_every_emitted_row(self):
        # Schema stability: `trigger` and `recommended_action` are emitted
        # unconditionally and this is the same shape, so consumers never have
        # to distinguish "absent key" from "no owner".
        tree = self._oversized_tree()
        # No owner resolves, so every key below comes from the row template
        # itself and not from the annotation step that fills it in.
        self._patch_join(lambda stems: {s: None for s in stems})
        for cand in tree_engine.get_distill_candidates(tree):
            self.assertIn("owned_by", cand)
            self.assertIn("owner_match", cand)
            self.assertIn("owner_open", cand)

    # ── the consumer half: the skill text that reads these fields ────────

    def _owner_strength_paragraph(self):
        """OWNER STRENGTH paragraph of /tree maintain, whitespace-normalised."""
        skill_path = MODULE_PATH.parents[2] / ".claude" / "skills" / "tree" / "SKILL.md"
        skill = " ".join(skill_path.read_text(encoding="utf-8").split())
        marker = "OWNER STRENGTH (g-115-10095"
        self.assertIn(marker, skill, "the consumer lost its owner-strength paragraph")
        return skill.split(marker, 1)[1].split("# Rationale", 1)[0]

    def test_consumer_names_exactly_the_strength_fields_a_row_carries(self):
        # : the remedy lives in two files. A rename on either side
        # leaves the other instructing a field no row carries, and a reader that
        # finds neither falls back to "cite that goal" for every non-null
        # owned_by, which is the false positive this goal fixed.
        self._patch_join(lambda stems: {s: None for s in stems})
        emitted = {k for k in tree_engine.get_distill_candidates(
            self._oversized_tree())[0] if k.startswith("owner_")}
        named = set(re.findall(r"\bowner_[a-z]+\b", self._owner_strength_paragraph()))
        self.assertTrue(emitted, "fixture must emit at least one owner_* field")
        self.assertEqual(named, emitted)

    def test_consumer_keeps_the_rules_that_decide_a_skip(self):
        # Each phrase is a rule a reader acts on; dropping one reopens the
        # false positive (4 of the top 10 links named non-owners, 2026-09-16).
        paragraph = self._owner_strength_paragraph()
        for phrase in (
                'owner_match == "title"',      # the only strength that licenses a skip
                "description-only match",       # a mention is not a claim
                "sweep or census goal",         # a list that contains the node
                "terminal owner is NOT an owner",   # nobody is on it
                "strength unknown",             # null beside a non-null owned_by
                "a PLAN, not evidence the work is under way",   # an id is a pointer
                "owned_by_open_goal_annotated_not_recensused",  # no longer a skip reason
        ):
            self.assertIn(phrase, paragraph)


if __name__ == "__main__":
    unittest.main()
