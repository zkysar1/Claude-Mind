"""test_completed_not_committed_tier1_merged_pr.py — regression for .

TIER 1 (`committed_not_pushed`, remedy "Push the commit") had no merged-pull-
request check. `all_merged_on_default` already encoded exactly that carve-out and
was wired ONLY to `classify_stranded` — i.e. only to OFF-DEFAULT shas, commits
still reachable from some remote branch. That premise has no answer once the
branch is DELETED, and a forge deletes the source branch on merge BY DEFAULT. So
a squash-merged branch's sha stops being off-default the moment it is auto-deleted
and becomes reachable from no remote ref at all — landing in tier 1, where no
merged-PR check ran. The carve-out protected the TRANSIENT state and missed the
STEADY one, decaying to zero coverage exactly as a repo tidies up after itself.

Measured (g-115-6781): a pull request MERGED as a squash (parent_count=1), head
branch deleted, head sha reachable from no remote branch, all five touched files
byte-identical between that sha and the merge commit, merge commit an ancestor of
the default branch. Tier 1 filed a HIGH Investigate reading "Push the commit (or
re-do the work if it was lost)" — impossible on its first clause (the branch is
gone) and destructive on its second (the work is live).

Tested as a PROPERTY, not against that one instance (guard-3080), and in BOTH
directions per guard-1194: the corrector must CLEAR the merged-and-shipped shape
AND must still leave a genuinely-lost deliverable flagged. The refusal cases
outnumber the blessing case deliberately — this lane SUPPRESSES, so its failure
mode is hiding the real deliverable loss the whole sweep exists to catch
(rb-3135 / g-115-2570).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_tier1_merged", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

HEAD_SHA = "5fc2b7d000111122223333444455556666777788"
OTHER_SHA = "abcdef00112233445566778899aabbccddeeff00"
MERGE_SHA = "043926a2ffeeddccbbaa99887766554433221100"
OTHER_MERGE = "1111222233334444555566667777888899990000"


def _entry(shas=(HEAD_SHA,), reason="committed_not_pushed"):
    """A tier-1 flagged entry exactly as classify_goal emits one."""
    return {
        "goal_id": "g-335-1141",
        "source": "world",
        "aspiration_id": "asp-335",
        "completed_at": "2026-08-15T10:00:00",
        "age_hours": 96.0,
        "reason": reason,
        "shas_absent_local_only": list(shas),
        "title": "add the usage page",
    }


def _pr(state="MERGED", merge_sha=MERGE_SHA, number=185):
    return {"state": state, "number": number,
            "url": f"https://example.invalid/pull/{number}",
            "title": "usage page", "created_at": "2026-08-14T09:00:00",
            "merge_commit_sha": merge_sha, "draft": False}


def _apply(entry, pr_status, merge_default_status):
    return cnc.apply_merged_pr_tier1(entry, pr_status, merge_default_status)


# --- the fix itself: the blessing direction --------------------------------

def test_merged_pr_on_default_clears_the_push_remedy():
    entry = _entry()
    assert entry["reason"] == "committed_not_pushed", (
        "positive control: before the corrector runs this entry carries the "
        "reason --apply files a HIGH Investigate for")
    _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: True})
    assert entry["reason"] == "benign_merged_pr", (
        "a MERGED pull request whose merge commit is on the default branch "
        "means the work shipped under a rewritten sha. The head sha is "
        "unreachable because the branch was deleted, not because the "
        f"deliverable is missing. Got: {entry['reason']}")


def test_blessed_entry_stays_in_the_report_with_its_evidence():
    """NOT a suppressor — the reader must still be able to audit the call, and
    the stranded branch may still want closing."""
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr(number=185)}, {MERGE_SHA: True})
    assert entry["shas_absent_local_only"] == [HEAD_SHA]
    assert entry["merged_pull_requests"] == [185]
    assert entry["goal_id"] == "g-335-1141"


def test_every_sha_explained_by_its_own_merged_pr():
    entry = _entry(shas=(HEAD_SHA, OTHER_SHA))
    _apply(entry,
           {HEAD_SHA: _pr(number=185),
            OTHER_SHA: _pr(merge_sha=OTHER_MERGE, number=186)},
           {MERGE_SHA: True, OTHER_MERGE: True})
    assert entry["reason"] == "benign_merged_pr"
    assert entry["merged_pull_requests"] == [185, 186]


# --- refusals: sensitivity must survive the fix ----------------------------

def test_open_pr_stays_flagged():
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr(state="OPEN")}, {MERGE_SHA: True})
    assert entry["reason"] == "committed_not_pushed"


def test_closed_unmerged_pr_stays_flagged():
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr(state="CLOSED")}, {MERGE_SHA: True})
    assert entry["reason"] == "committed_not_pushed"


def test_no_pull_request_at_all_stays_flagged():
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr(state="NONE", merge_sha=None)}, {})
    assert entry["reason"] == "committed_not_pushed"


def test_unavailable_forge_probe_stays_flagged():
    """An unreachable forge must never convert a flagged sweep into a clean one
    (g-115-3471) — this is verify-before-assuming rule 4 at the suppression
    boundary, where a silent failure would read as a blessing."""
    entry = _entry()
    _apply(entry, {HEAD_SHA: dict(cnc._PR_UNAVAILABLE)}, {})
    assert entry["reason"] == "committed_not_pushed"


def test_merge_commit_not_on_default_stays_flagged():
    """Merged into a NON-default base (a release train, a stacked PR). The work
    genuinely has not reached the default branch."""
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: False})
    assert entry["reason"] == "committed_not_pushed"


def test_unresolvable_merge_commit_stays_flagged():
    """None is the shape a merge commit takes in an unfetched clone. Staleness
    must not manufacture a blessing (rb-4716)."""
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: None})
    assert entry["reason"] == "committed_not_pushed"


def test_null_merge_commit_sha_stays_flagged():
    entry = _entry()
    _apply(entry, {HEAD_SHA: _pr(merge_sha=None)}, {MERGE_SHA: True})
    assert entry["reason"] == "committed_not_pushed"


def test_partial_explanation_keeps_the_whole_entry_flagged():
    """One unexplained sha is enough. Requiring ALL (not any) mirrors
    apply_superseded and all_merged_on_default — a real deliverable loss must
    not ride out on a sibling sha's merged pull request."""
    entry = _entry(shas=(HEAD_SHA, OTHER_SHA))
    _apply(entry,
           {HEAD_SHA: _pr(), OTHER_SHA: _pr(state="OPEN", number=186)},
           {MERGE_SHA: True})
    assert entry["reason"] == "committed_not_pushed"


def test_unprobed_sha_declines_rather_than_being_skipped():
    """A sha with NO record must not be silently passed over — blessing an entry
    on the strength of only the shas that happened to be probed is the one
    direction this must never fail."""
    entry = _entry(shas=(HEAD_SHA, OTHER_SHA))
    _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: True})
    assert entry["reason"] == "committed_not_pushed"


def test_empty_sha_set_is_not_benign():
    entry = _entry(shas=())
    _apply(entry, {}, {})
    assert entry["reason"] == "committed_not_pushed"


# --- ordering: defer to the more specific correctors -----------------------

def test_superseded_entry_is_left_alone():
    """apply_superseded ran first and found the content in HEAD — a stronger and
    more useful statement than 'a pull request carrying it merged'."""
    entry = _entry(reason="benign_superseded")
    _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: True})
    assert entry["reason"] == "benign_superseded"
    assert "merged_pull_requests" not in entry


def test_misrouted_reachability_verdict_is_left_alone():
    """ABSENT / STRANDED_WORKER_REF are statements about the sha itself."""
    for reason in cnc._MISROUTED_VERDICTS.values():
        entry = _entry(reason=reason)
        _apply(entry, {HEAD_SHA: _pr()}, {MERGE_SHA: True})
        assert entry["reason"] == reason


# --- the tier-2 lane must be untouched -------------------------------------

def test_tier2_predicate_is_reused_not_reimplemented():
    """The whole point of the fix is that the carve-out already existed. If a
    second copy of the predicate ever appears, this catches the divergence."""
    assert cnc.all_merged_on_default([_pr()], {MERGE_SHA: True}) is True
    assert cnc.all_merged_on_default([_pr(state="OPEN")], {MERGE_SHA: True}) is False
    assert cnc.all_merged_on_default([], {}) is False
