"""A sha scraped out of a RETRACTION sentence must not be attributed to the goal
whose prose contains it (g-115-6115).

extract_commit_shas anchors on a verb cue (commit/pushed/merged/sha) to reject
free-floating hex. The anchor works. What a regex cannot do is tell an ASSERTION
of authorship from its RETRACTION or from a CITATION of someone else's commit --
the same verb appears in all three:

    "Committed a409a3d7e and pushed to origin/main."          <- assertion
    "I read a409a3d7e as uncarried and was WRONG."            <- retraction
    "The stranded commit is a409a3d7e (filed by g-350-187)."  <- citation

Two consequences, and the second is the reason this was raised to HIGH:

  1. A careful agent documenting a self-correction plants a token that is then
     attributed to it. The better the write-up, the more likely the misattribution.
  2. It is SELF-PERPETUATING. A stranded-commit Investigate cites the sha as
     evidence, thereby acquiring it in its own commit scope, and is flagged in
     turn -- forever. Measured chain on this box: g-350-187 -> g-115-6275 ->
     g-115-6359. Nothing ages it out, because each link regenerates its own
     in-window member.

FIXTURE SUBSTITUTION -- STATED, NOT PAPERED OVER. The goal's verification
criterion names "the g-326-82 outcome_note" as the demonstration fixture. That
text is UNRECOVERABLE on this box: g-326-82 was EVICTED (it is present in
asp-326's archived_census.evicted_ids.completed, 132 of 144, but `"id":
"g-326-82"` returns 0 occurrences across the world store, the archive, and every
agent queue). The census kept the id and discarded the prose. So RETRACTION
below is a SYNTHETIC fixture in that goal's documented shape, and the two live
chain instances (CHAIN_A / CHAIN_B) carry the real-estate evidence instead --
they are measured, not invented, and they are the population the remedy was
sized against. A verification criterion that names a live record as its fixture
is a time-bomb; that is a finding of this goal, not an excuse in it.

MEASURED against the live estate 2026-08-16 (cc-07, uname -r 6.8.0-137-generic,
60 candidate repos, 2,535 goals scanned): 17 unique (goal, sha) attributions.
17/17 commits carried a parenthesized goal-id; 10 named the flagged goal and
were kept; 7 named a different goal, of which exactly 2 sit in the ACTIONABLE
classes -- both chain links, zero genuine attributions lost.

WHAT MUST NOT CHANGE is the asymmetry. Rejection requires POSITIVE evidence of
different ownership. A commit naming NO goal-id is left ALONE -- deliberately
weaker than "require the flagged commit to name this goal", because the hard
form introduces a false negative for every hand-made commit, and a false
negative in this sweep is worse than the false positive being fixed. The
no-goal-id test below is the pin on that, and it is the one to check first if
anyone tightens this.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_retraction", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

NOW = datetime(2026, 8, 16, 12, 0, 0)
TWO_H_AGO = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

# ── the two LIVE chain instances (measured 2026-08-16, cc-07) ──────────────
# Both are actionable-class false positives the filter now rejects.
CHAIN_A_SHA = "a409a3d7e"          # commit names  / 
CHAIN_A_GOAL = "g-115-6275"        # the Investigate that merely CITED it
CHAIN_B_SHA = "67dbafe6"           # commit names 
CHAIN_B_GOAL = "g-115-6280"

# Synthetic, in 's documented shape (see module docstring on why the
# real text is unrecoverable). The verb cue "commit" is present, which is
# exactly why extract_commit_shas scrapes it.
RETRACTION = (
    "CORRECTION: I earlier read commit a409a3d7e as uncarried and was WRONG -- "
    "it was carried, by g-350-187, and this goal shipped nothing of its own.")

# A citation of a partner's work. Same verb, opposite meaning.
#
# ADJACENCY IS A REAL PARTIAL MITIGATION -- measured here, not assumed. The
# anchor is `commit[\s:=@,]*<hex>`, so "the stranded commit IS 67dbafe6"
# scrapes NOTHING (the intervening word breaks it) while "the stranded commit
# 67dbafe6" scrapes fine. So the leak is narrower than "any sentence with a
# verb and a hex token" -- but it is not closed, because the adjacent phrasing
# is the more natural one and is what both live chain links actually used.
CITATION = "The stranded commit 67dbafe6 was filed under g-335-1212."

# The honest assertion the sweep exists to act on.
ASSERTION = "Committed f885a690 and pushed to origin/main."


def _goal(**kw):
    g = {
        "id": "g-350-99",
        "status": "completed",
        "work_class": "product",
        "completed_at": TWO_H_AGO,
        "_source": "world",
        "_aspiration_id": "asp-350",
        "title": "Fix: unit-tree root-field dual-read",
        "outcome_note": ASSERTION,
        "verification": {"summary": "full suite green"},
    }
    g.update(kw)
    return g


# ── own_shas: the pure filter ─────────────────────────────────────────────

def test_retraction_sha_is_not_attributed_to_the_retracting_goal():
    """OUTCOME 1. The goal's prose contains the sha; the COMMIT names someone
    else; the attribution is dropped."""
    shas = cnc.extract_commit_shas(_goal(id="g-326-82", outcome_note=RETRACTION))
    assert CHAIN_A_SHA in shas, (
        "non-vacuity: the extractor must still SCRAPE the retraction sha -- "
        "if it stopped, this test would pass for the wrong reason")
    kept = cnc.own_shas("g-326-82", shas, {CHAIN_A_SHA: ["g-350-187"]})
    assert CHAIN_A_SHA not in kept
    assert kept == []


def test_citation_of_a_partners_commit_is_not_attributed():
    shas = cnc.extract_commit_shas(_goal(id="g-115-9999", outcome_note=CITATION))
    assert CHAIN_B_SHA in shas, "non-vacuity: extractor must scrape the citation"
    kept = cnc.own_shas("g-115-9999", shas, {CHAIN_B_SHA: ["g-335-1212"]})
    assert kept == []


def test_live_chain_instance_a_is_rejected():
    """ cited a409a3d7e, which names /."""
    kept = cnc.own_shas(CHAIN_A_GOAL, [CHAIN_A_SHA],
                        {CHAIN_A_SHA: ["g-350-187", "g-350-207"]})
    assert kept == []


def test_live_chain_instance_b_is_rejected():
    """ cited 67dbafe6, which names ."""
    kept = cnc.own_shas(CHAIN_B_GOAL, [CHAIN_B_SHA], {CHAIN_B_SHA: ["g-335-1212"]})
    assert kept == []


def test_a_commit_naming_the_flagged_goal_is_KEPT():
    """The 10-of-17 majority case. Rejecting these would blind the sweep."""
    kept = cnc.own_shas("g-350-99", ["f885a690"], {"f885a690": ["g-350-99"]})
    assert kept == ["f885a690"]


def test_a_commit_naming_this_goal_AMONG_OTHERS_is_KEPT():
    """Multi-goal commit subjects are common; membership, not equality."""
    kept = cnc.own_shas("g-350-99", ["f885a690"],
                        {"f885a690": ["g-115-1", "g-350-99"]})
    assert kept == ["f885a690"]


def test_a_commit_naming_NO_goal_id_is_left_ALONE():
    """THE ASYMMETRY PIN -- check this one first if anyone tightens the filter.

    A hand-made commit with no goal-id in its subject is the population the
    hard form ("require the flagged commit to name this goal") would silently
    start missing. A false negative here means a goal closed `completed` with
    code that never reached the default branch and nothing says so.
    """
    kept = cnc.own_shas("g-350-99", ["f885a690"], {"f885a690": []})
    assert kept == ["f885a690"]


def test_an_UNKNOWN_sha_is_left_ALONE():
    """Absent from the owners map (repo not probed / sha unresolved) is the
    same epistemic state as 'names no goal-id': no positive evidence."""
    kept = cnc.own_shas("g-350-99", ["deadbeef"], {"f885a690": ["g-115-1"]})
    assert kept == ["deadbeef"]


def test_empty_owners_map_is_a_passthrough():
    """Backward compatibility: every caller that does not inject ownership
    (and every pre-existing test) must see the unfiltered list."""
    assert cnc.own_shas("g-350-99", ["a", "b"], {}) == ["a", "b"]
    assert cnc.own_shas("g-350-99", ["a", "b"], None) == ["a", "b"]


def test_missing_goal_id_cannot_reject():
    """A goal with no id has nothing to compare against -- must not become a
    blanket rejection of every owned sha."""
    kept = cnc.own_shas(None, [CHAIN_A_SHA], {CHAIN_A_SHA: ["g-350-187"]})
    assert kept == [CHAIN_A_SHA]


def test_mixed_list_drops_only_the_foreign_sha():
    kept = cnc.own_shas(
        "g-350-99", [CHAIN_A_SHA, "f885a690", "deadbeef"],
        {CHAIN_A_SHA: ["g-350-187"], "f885a690": ["g-350-99"], "deadbeef": []})
    assert kept == ["f885a690", "deadbeef"]


# ── _COMMIT_GOALID_RE: the ownership extractor ────────────────────────────

def test_goalid_regex_reads_the_conventional_commit_scope():
    assert cnc._COMMIT_GOALID_RE.findall(
        "fix(g-335-1212): key the absence on the claim anchor") == ["g-335-1212"]


def test_goalid_regex_reads_a_body_line_too():
    found = cnc._COMMIT_GOALID_RE.findall(
        "docs(g-115-6217): note the strand\n\nsupersedes (g-350-187)")
    assert found == ["g-115-6217", "g-350-187"]


def test_goalid_regex_ignores_an_unparenthesised_mention():
    """Bare prose mentions are NOT ownership -- a commit body that discusses
    g-350-187 has not been authored by it. Requiring the parens is what keeps
    the filter's evidence POSITIVE."""
    assert cnc._COMMIT_GOALID_RE.findall("relates to g-350-187 somehow") == []


def test_goalid_regex_handles_four_digit_ids():
    """Goal ids are 2-4 digit (CLAUDE.md ID Formats); asp-115 passed 
    in 2026-05."""
    assert cnc._COMMIT_GOALID_RE.findall("feat(g-115-6115): x") == ["g-115-6115"]


# ── classify_goal: the wiring ─────────────────────────────────────────────

def test_classify_goal_flags_the_retraction_goal_WITHOUT_the_filter():
    """Positive control. If this ever stops flagging, the test below passes
    vacuously and the filter is proved by nothing."""
    g = _goal(id="g-326-82", outcome_note=RETRACTION)
    entry = cnc.classify_goal(g, NOW, {CHAIN_A_SHA: False})
    assert entry is not None
    assert CHAIN_A_SHA in entry["shas_absent_local_only"]


def test_classify_goal_does_not_flag_once_ownership_is_injected():
    """The same goal, same sha_status, plus ownership -> no finding."""
    g = _goal(id="g-326-82", outcome_note=RETRACTION)
    entry = cnc.classify_goal(g, NOW, {CHAIN_A_SHA: False},
                              sha_goalid_owners={CHAIN_A_SHA: ["g-350-187"]})
    assert entry is None


def test_classify_goal_still_flags_a_genuinely_owned_unlanded_commit():
    """The detection this sweep exists for must survive the filter."""
    g = _goal(id="g-350-99", outcome_note=ASSERTION)
    entry = cnc.classify_goal(g, NOW, {"f885a690": False},
                              sha_goalid_owners={"f885a690": ["g-350-99"]})
    assert entry is not None
    assert entry["shas_absent_local_only"] == ["f885a690"]


def test_classify_goal_still_flags_when_the_commit_names_no_goal():
    """The asymmetry, end to end: no ownership evidence -> detection stands."""
    g = _goal(id="g-350-99", outcome_note=ASSERTION)
    entry = cnc.classify_goal(g, NOW, {"f885a690": False},
                              sha_goalid_owners={"f885a690": []})
    assert entry is not None
