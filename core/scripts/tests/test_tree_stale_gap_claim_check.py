"""test_tree_stale_gap_claim_check.py — .

Regression tests for core/scripts/tree-stale-gap-claim-check.py, which surfaces
tree nodes stating a present-tense capability gap while citing a goal that has
since gone TERMINAL.

In-process against the module's PURE core (split_front_matter / scan_node /
citation_direction / classify / build_goal_status_index with an injected
aspiration iterator). Deliberately NOT subprocess-based like its sibling
test_tree_last_updated_drift_check.py: this script's index comes from the
daemon-backed aspirations stores, and a subprocess test would either need a live
daemon or would silently exercise the empty-index path — which is exactly the
"reads clean because it read nothing" failure the script is written against.

Each test is falsified by a DISTINCT wrong implementation:
  * scan front matter too                  -> test_front_matter_is_not_scanned
  * compute direction on truncated text    -> test_citation_direction_uses_the_FULL_line
  * resolve only the live goals list       -> test_evicted_goal_status_resolves
  * treat any cited goal as terminal       -> test_open_goal_is_not_contradicted
  * report unknown ids as contradictions   -> test_unresolvable_citation_is_its_own_bucket
  * silently drop retracted lines          -> test_retracted_line_is_flagged_not_dropped
  * key --exit-on-hits to `contradicted`   -> test_review_set_is_the_forward_pointer_subset
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TARGET = CORE_SCRIPTS / "tree-stale-gap-claim-check.py"

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location("tree_stale_gap_claim_check", TARGET)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)


# --- corpus shape -----------------------------------------------------------

def test_front_matter_is_not_scanned():
    """MENTION vs CLAIM: a goal-id in `cross_refs:` is provenance, not a gap
    claim. 1310 of 3053 live nodes carry a goal-id ONLY in front matter, so
    scanning it would swamp the signal."""
    text = (
        "---\n"
        "topic: x\n"
        "cross_refs:\n"
        "  - g-115-2889\n"
        "note: nothing invokes it\n"
        "---\n"
        "\nOrdinary body prose with no claim.\n"
    )
    fm, body = gap.split_front_matter(text)
    assert "g-115-2889" in fm
    assert "g-115-2889" not in body
    assert list(gap.scan_node(Path("n.md"), body)) == []


def test_body_gap_claim_with_goal_id_is_a_hit():
    body = "\n- The audit is NOT wired to a recurring cadence; prevention g-115-2889\n"
    hits = list(gap.scan_node(Path("n.md"), body))
    assert len(hits) == 1
    assert hits[0]["cited_goal_ids"] == ["g-115-2889"]
    assert "is_not_participle" in hits[0]["patterns"]


def test_a_goal_id_with_no_absence_phrasing_is_not_a_hit():
    """Guards the other direction: the absence phrasing carries the signal, so a
    bare provenance line must not become a hit."""
    body = "\n- Shipped the retry path -- source: g-115-2889\n"
    assert list(gap.scan_node(Path("n.md"), body)) == []


# --- the truncation regression ---------------------------------------------

def test_citation_direction_uses_the_FULL_line():
    """THE REGRESSION. `text` is truncated to 400 chars for output, but these
    corpus bullets routinely exceed that. Computing direction over the truncated
    copy returned "neither" whenever the pointer sat past the cut, which dropped
    a KNOWN true positive (tree-maintenance-patterns, `prevention g-115-2889`)
    out of the review set entirely."""
    filler = "x" * 500
    line = ("- The audit is NOT wired to a recurring cadence. " + filler +
            " rb-4597; prevention g-115-2889 (wire recurring detect+repair)")
    hits = list(gap.scan_node(Path("n.md"), "\n" + line + "\n"))
    assert len(hits) == 1
    assert len(hits[0]["text"]) <= 400, "output text should stay truncated"
    assert "g-115-2889" not in hits[0]["text"], "fixture must place the id past the cut"

    index = {"g-115-2889": {"status": "completed", "asp_id": "asp-115",
                            "origin": "world-evicted"}}
    contradicted, _open, _unres = gap.classify(hits, index)
    assert len(contradicted) == 1
    assert contradicted[0]["citation_direction"] in ("forward", "both"), (
        "direction was computed over the truncated text, not the full line")
    assert "_full_line" not in contradicted[0], "internal key must not be emitted"


def test_citation_direction_buckets():
    assert gap.citation_direction("gap -> g-1-1", {"g-1-1"}) == "forward"
    assert gap.citation_direction("gap -- source: g-1-1", {"g-1-1"}) == "backward"
    # Both cues must sit BEFORE the id: the window is the 60 chars PRECEDING it,
    # so a cue after the id is deliberately out of scope.
    assert gap.citation_direction("source: it, prevention g-1-1", {"g-1-1"}) == "both"
    assert gap.citation_direction("prevention g-1-1 -- source: after",
                                  {"g-1-1"}) == "forward", (
        "a cue AFTER the id must not count -- the window is preceding-only")
    assert gap.citation_direction("bare g-1-1", {"g-1-1"}) == "neither"
    # A non-terminal id must not contribute a direction.
    assert gap.citation_direction("gap -> g-9-9", {"g-1-1"}) == "neither"


# --- status resolution ------------------------------------------------------

def _hit(text="- nothing invokes it; see g-1-1", ids=("g-1-1",)):
    return {
        "node": "n.md", "line": 1, "patterns": ["nothing_verbs"],
        "cited_goal_ids": list(ids), "text": text, "_full_line": text,
        "retraction_markers": False, "prior_set_a": False, "prior_set_b": False,
    }


def test_open_goal_is_not_contradicted():
    contradicted, open_gap, unres = gap.classify(
        [_hit()], {"g-1-1": {"status": "in-progress"}})
    assert (len(contradicted), len(open_gap), len(unres)) == (0, 1, 0)


def test_terminal_goal_is_contradicted():
    contradicted, _o, _u = gap.classify(
        [_hit()], {"g-1-1": {"status": "completed"}})
    assert len(contradicted) == 1
    assert contradicted[0]["terminal_goal_ids"] == ["g-1-1"]


def test_unresolvable_citation_is_its_own_bucket():
    """An id resolving to nothing must NOT be reported as a contradiction — that
    is the shape a partial index produces, and calling it a hit would
    manufacture findings from an incomplete read."""
    contradicted, open_gap, unres = gap.classify([_hit()], {})
    assert (len(contradicted), len(open_gap), len(unres)) == (0, 0, 1)


def test_evicted_goal_status_resolves(monkeypatch):
    """THE RESOLUTION TRAP. Aged terminal goals are EVICTED from the live goals
    list, so a live-only lookup reports not-found for precisely the population
    this detector hunts. Measured on the live store: 10047 of 15649 indexed ids
    come from the eviction census."""
    live_only = {"id": "asp-1", "goals": [{"id": "g-1-1", "status": "pending"}]}
    evicted = {
        "id": "asp-2",
        "goals": [],
        "archived_census": {"evicted_ids": {"completed": ["g-2-2"]}},
    }

    def fake_iter(errors):
        yield "world", live_only
        yield "world-archive", evicted

    monkeypatch.setattr(gap, "_iter_aspirations", fake_iter)
    index, errors, counts = gap.build_goal_status_index()
    assert errors == {}
    assert index["g-1-1"]["status"] == "pending"
    assert index["g-2-2"]["status"] == "completed", "evicted goal did not resolve"
    assert counts == {"live": 1, "evicted": 1}


def test_live_record_wins_over_a_stale_eviction_census(monkeypatch):
    """An id in BOTH a live list and a census must take the live status: the
    census is a tombstone record and can lag."""
    asp = {
        "id": "asp-1",
        "goals": [{"id": "g-1-1", "status": "in-progress"}],
        "archived_census": {"evicted_ids": {"completed": ["g-1-1"]}},
    }

    def fake_iter(errors):
        yield "world", asp

    monkeypatch.setattr(gap, "_iter_aspirations", fake_iter)
    index, _e, _c = gap.build_goal_status_index()
    assert index["g-1-1"]["status"] == "in-progress"


# --- reporting discipline ---------------------------------------------------

def test_retracted_line_is_flagged_not_dropped():
    """Assertion-vs-retraction is not lexically decidable, so a retracted-looking
    line is FLAGGED and still reported (guard-4664) — never filtered out."""
    body = "\n- ~~nothing invokes it~~ RESOLVED by g-1-1\n"
    hits = list(gap.scan_node(Path("n.md"), body))
    assert len(hits) == 1, "a retracted line must still be reported"
    assert hits[0]["retraction_markers"] is True


def test_prior_pattern_sets_are_recorded_for_the_recall_measurement():
    """The recall comparison against 's two sets needs per-hit flags."""
    seen = list(gap.scan_node(Path("n.md"), "\n- nothing invokes it, see g-1-1\n"))[0]
    assert seen["prior_set_a"] is True, "'nothing invokes' is in prior set A"
    missed = list(gap.scan_node(
        Path("n.md"), "\n- there is currently no scheduler for it, see g-1-1\n"))[0]
    assert missed["prior_set_a"] is False and missed["prior_set_b"] is False, (
        "an existential phrasing must be counted as missed by the prior sets")


def test_review_set_is_the_forward_pointer_subset():
    """--exit-on-hits keys to the review set, not to `contradicted`: 76.5% of ALL
    goal-citing tree lines cite a terminal goal, so keying to `contradicted`
    would fire every run forever."""
    fwd = _hit(text="- nothing invokes it -> g-1-1")
    bwd = _hit(text="- nothing invokes it -- source: g-1-1")
    contradicted, _o, _u = gap.classify([fwd, bwd], {"g-1-1": {"status": "completed"}})
    review = [h for h in contradicted
              if h.get("citation_direction") in ("forward", "both")]
    assert len(contradicted) == 2, "both remain reported"
    assert len(review) == 1, "only the forward pointer is in the triage set"
