"""test_completed_not_committed_squash_merge.py — regression for .

`classify_stranded` asks whether the goal's OWN commit sha is contained by the
default branch. Under a squash- or rebase-merge that answer is permanently NO,
by construction: the forge discards the branch commit and writes a new one. So
the most conclusive evidence a product goal can emit — a MERGED pull request —
scored identically to an abandoned branch and landed in `stranded_no_pr`
forever. Measured on a live fleet run 2026-08-12: 36 of 51 `stranded_no_pr`
entries were already-merged pull requests (4 spot-verified by hand against the
forge), a 71% false-positive rate that buried the 15 entries worth reading.

Tested as a PROPERTY, not against those 36 instances (guard-3080): a merged PR
whose merge commit is on the default branch is benign, whatever the goal or repo.

The suppression direction is what makes these tests load-bearing. Every OTHER
lane in this sweep is conservative toward not-flagging, so its failure mode is
silence. This one SUPPRESSES, so its failure mode is hiding a genuinely stranded
deliverable — which is the defect the whole sweep exists to catch. Hence the
larger share of cases below asserting that a blessing is REFUSED.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_squash", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

NOW = datetime.datetime(2026, 8, 12, 12, 0, 0)
BRANCH_SHA = "aaaaaaaabbbbbbbbccccccccdddddddd11111111"
MERGE_SHA = "99999999888888887777777766666666ffffffff"


def _goal(sha=BRANCH_SHA, gid="g-335-1131"):
    """A completed code-deliverable goal naming one commit sha, aged past
    --min-age-minutes so the eligibility ladder lets it through."""
    return {
        "id": gid,
        "_source": "world",
        "_aspiration_id": "asp-335",
        "status": "completed",
        "completed_at": "2026-08-12T06:00:00",
        "title": "ship the widget endpoint",
        "verification": {"outcomes": [f"commit {sha}"]},
    }


def _pr(state="MERGED", merge_sha=MERGE_SHA, number=54):
    return {"state": state, "number": number,
            "url": f"https://example.invalid/pull/{number}",
            "title": "widget endpoint", "created_at": "2026-08-05T09:00:00",
            "merge_commit_sha": merge_sha}


def _classify(pr_record, merge_default_status):
    """Drive the full tier-2 ladder with the branch sha OFF the default branch —
    the shape every squash-merged goal has."""
    return cnc.classify_stranded(
        _goal(), NOW,
        sha_status={BRANCH_SHA: True},          # landed on some remote branch
        default_status={BRANCH_SHA: False},     # ...but not the default branch
        pr_status={BRANCH_SHA: pr_record},
        merge_default_status=merge_default_status)


# --- the fix itself --------------------------------------------------------

def test_squash_merged_pr_is_benign_not_stranded():
    entry = _classify(_pr(), {MERGE_SHA: True})
    assert entry is not None, "the entry must stay in the report, not vanish"
    assert entry["reason"] == "benign_squash_merged", (
        "a MERGED pull request whose merge commit is on the default branch is "
        "not stranded -- the work shipped under a rewritten sha. Scoring it "
        f"stranded_no_pr is the 71% false-positive class. Got: {entry['reason']}")


def test_benign_entry_still_names_its_pull_request():
    """The carve-out must not cost the reader the evidence. A bucket he cannot
    audit is how the previous over-trusted claim survived so long."""
    entry = _classify(_pr(number=129), {MERGE_SHA: True})
    assert entry["pull_request"]["number"] == 129
    assert entry["shas_off_default"] == [BRANCH_SHA]


# --- refusals: every uncertainty must decline to bless ---------------------

def test_merge_commit_not_on_default_stays_stranded():
    """Merged into a NON-default base (a release train, a stacked PR). The work
    genuinely has not reached the default branch."""
    assert _classify(_pr(), {MERGE_SHA: False})["reason"] == "stranded_no_pr"


def test_unresolvable_merge_commit_stays_stranded():
    """None = the prober could not find that sha in any candidate repo, which is
    exactly what an UNFETCHED local clone looks like (rb-4716). Staleness must
    never manufacture a blessing, so None is not True."""
    assert _classify(_pr(), {MERGE_SHA: None})["reason"] == "stranded_no_pr"


def test_null_merge_commit_sha_stays_stranded():
    """Some CLOSED-then-merged records carry a null merge_commit_sha. No sha
    means no evidence, and no evidence means no blessing."""
    assert _classify(_pr(merge_sha=None), {})["reason"] == "stranded_no_pr"


def test_closed_unmerged_pr_stays_stranded():
    """An abandoned branch is the case this bucket is FOR. If a CLOSED record
    could be blessed, the carve-out would swallow the very signal it protects."""
    assert _classify(_pr(state="CLOSED", merge_sha=None),
                     {})["reason"] == "stranded_no_pr"


def test_closed_pr_carrying_a_resolvable_merge_sha_stays_stranded():
    """The state check is load-bearing on its OWN, independently of the sha
    checks that follow it — and nothing proved that until mutation testing.

    Deleting `state != "MERGED"` left the whole suite green, because every other
    not-merged case here also had a null merge_commit_sha, so the NEXT guard
    silently covered for the missing one. A guard no test can kill is a guard
    that will be refactored away by someone who sees it as redundant.

    The case is real, not synthetic: GitHub populates merge_commit_sha on
    closed-unmerged pull requests with the sha of a TEST-merge commit, which
    never lands on the default branch but is a perfectly resolvable object. If
    such a sha ever resolves True, only the state check stands between an
    abandoned branch and a blessing."""
    assert _classify(_pr(state="CLOSED"),
                     {MERGE_SHA: True})["reason"] == "stranded_no_pr"
    assert cnc.all_merged_on_default([_pr(state="CLOSED")],
                                     {MERGE_SHA: True}) is False


def test_no_pull_request_at_all_stays_stranded():
    assert _classify({"state": "NONE", "number": None, "url": None,
                      "title": None, "created_at": None,
                      "merge_commit_sha": None}, {})["reason"] == "stranded_no_pr"


def test_partial_explanation_keeps_the_whole_entry_stranded():
    """ALL off-default shas must be explained, mirroring apply_superseded's
    conservative direction: one unexplained sha keeps the entry visible. A goal
    spanning two repos can be merged in one and abandoned in the other."""
    other = "1234567890abcdef1234567890abcdef12345678"
    goal = _goal()
    goal["verification"]["outcomes"].append(f"commit {other}")
    entry = cnc.classify_stranded(
        goal, NOW,
        sha_status={BRANCH_SHA: True, other: True},
        default_status={BRANCH_SHA: False, other: False},
        pr_status={BRANCH_SHA: _pr(),
                   other: _pr(state="CLOSED", merge_sha=None, number=77)},
        merge_default_status={MERGE_SHA: True})
    assert entry["reason"] == "stranded_no_pr", (
        "one merged sha must not bless a sibling that was abandoned")


# --- backward compatibility: the carve-out cannot fire without evidence ----

def test_omitting_merge_default_status_preserves_legacy_behaviour():
    """Every pre- caller omits the new argument. Such a caller has
    supplied no containment evidence, so it must get the old verdict -- never a
    blessing inferred from the absence of data."""
    entry = cnc.classify_stranded(
        _goal(), NOW,
        sha_status={BRANCH_SHA: True},
        default_status={BRANCH_SHA: False},
        pr_status={BRANCH_SHA: _pr()})
    assert entry["reason"] == "stranded_no_pr"


def test_open_pr_lane_is_untouched():
    """The strong, Investigate-FILING lane must not be reachable by this change.
    An OPEN pull request carries no merge commit, so the predicate declines and
    stranded_open_pr still wins."""
    entry = _classify(_pr(state="OPEN", merge_sha=None), {MERGE_SHA: True})
    assert entry["reason"] == "stranded_open_pr"


# --- the predicate in isolation -------------------------------------------

def test_empty_record_set_is_not_benign():
    """`all(...)` over an empty sequence is True -- the classic vacuous-truth
    footgun. Here it would bless a goal with no pull-request evidence at all."""
    assert cnc.all_merged_on_default([], {MERGE_SHA: True}) is False
