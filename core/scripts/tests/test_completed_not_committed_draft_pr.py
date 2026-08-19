"""A DRAFT pull request must not be prescribed a merge ().

completed-not-committed-sweep tier 2 offered exactly two remedies for an
off-default commit: merge the pull request, or close it as abandoned. A DRAFT
pull request is neither. It is the author's deliberate "not ready" signal, and
merging on the sweep's advice ships a half-feature.

Measured origin: PR #425 on Ayoai-Public-Web-App carried the frontend half of a
self-serve API-key revoke/rotate feature whose two Lambdas were not deployed.
Its own body said verbatim "Merging this alone gives buttons that fail." The
sweep filed a HIGH goal reading "Merge the pull request (or close it and
re-open the goal if the work was abandoned)" — advice that would have shipped
controls calling services that do not exist.

What must NOT change is the DETECTION. A goal that closed `completed` while its
deliverable cannot ship is a genuine premature-close and is worth flagging
whether or not the PR is a draft. Suppressing draft PRs from the sweep would
hide exactly that class. Only the remedy forks — these tests pin both halves.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_draft", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

# The non-draft advice, pinned as a literal so that a future edit which reflows
# that branch fails here rather than silently changing what every non-draft flag
# carries.
#
# RETIRED 2026-08-15 (): the original literal was
#   "Merge the pull request (or close it and re-open the goal if the work was
#    abandoned)."
# and this pin did exactly its job — it went red on the edit that removed it,
# which is what forced the change to be deliberate. It was retired on purpose,
# not reflowed. A bare merge INSTRUCTION is unsafe from this sweep: it reasons
# only about branch containment and cannot see a repo's deploy constraints.
#  merged on advice where every conventional signal read green (23/23
# suite on the merge result, 0 conflicts, MERGEABLE/CLEAN, CI's tests job
# PASSED) and still broke a live place, because that repo's deploy path can
# UPDATE an existing script but cannot CREATE a new one. The remedy is now an
# observation plus a verification step. Keep this pinned as a literal.
NONDRAFT_REMEDY_ANCHOR = (
    "Before merging, verify the pull request is DEPLOYABLE for this repository")
RETIRED_BARE_MERGE_INSTRUCTION = (
    "Merge the pull request (or close it and re-open the goal "
    "if the work was abandoned).")


class _CapturingRT:
    """Stands in for the daemon runtime so _file_investigate composes its body
    without filing a real goal."""

    def __init__(self):
        self.bodies = []

    def aspirations_add_goal(self, asp_id, body, source=None):
        self.bodies.append(body)
        return {"id": "g-115-9999"}


def _entry(draft):
    """A stranded_open_pr entry. `draft` is threaded into the pull_request
    record exactly as probe_sha_pull_request would emit it."""
    pr = {"number": 425, "url": "https://example.invalid/pull/425",
          "age_hours": 39.0, "state": "OPEN"}
    if draft is not None:
        pr["draft"] = draft
    return {
        "goal_id": "g-335-1142",
        "source": "world",
        "title": "Add API key rotation and expiry management",
        "age_hours": 38.9,
        "reason": "stranded_open_pr",
        "shas_off_default": ["7896aa5470fd203e07798ac373b45448063ee75d"],
        "pull_request": pr,
    }


def _describe(monkeypatch, draft):
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    cnc._file_investigate(_entry(draft))
    assert len(rt.bodies) == 1
    return rt.bodies[0]["description"]


# ── the draft branch ────────────────────────────────────────────────────────

def test_draft_pr_is_not_told_to_merge(monkeypatch):
    d = _describe(monkeypatch, True)
    assert RETIRED_BARE_MERGE_INSTRUCTION not in d
    assert NONDRAFT_REMEDY_ANCHOR not in d
    assert "do NOT merge it" in d


def test_draft_pr_is_named_as_draft(monkeypatch):
    d = _describe(monkeypatch, True)
    assert "OPEN as a DRAFT after" in d


def test_draft_pr_routes_to_the_precondition(monkeypatch):
    """The remedy must send the reader at the blocking precondition — that is
    the whole point of the fork, not merely withholding the merge advice."""
    d = _describe(monkeypatch, True)
    assert "precondition" in d
    assert "Read the PR body" in d


def test_draft_pr_questions_the_completed_close(monkeypatch):
    """A draft behind a `completed` goal is a premature-close signal, and the
    remedy has to say so or the finding is merely suppressed."""
    d = _describe(monkeypatch, True)
    assert "cannot ship" in d


# ── the non-draft branch stays byte-identical ───────────────────────────────

def test_non_draft_gets_the_deployability_remedy(monkeypatch):
    d = _describe(monkeypatch, False)
    assert NONDRAFT_REMEDY_ANCHOR in d
    assert "DRAFT" not in d


def test_bare_merge_instruction_is_never_emitted(monkeypatch):
    """The retired literal must not come back on ANY branch. This sweep has no
    evidence a merge is safe, so it must never issue one as an instruction."""
    for draft in (True, False, None):
        assert RETIRED_BARE_MERGE_INSTRUCTION not in _describe(monkeypatch, draft)


def test_absent_draft_key_is_treated_as_non_draft(monkeypatch):
    """FAIL-SAFE DIRECTION. Entries composed before this change — and every
    UNAVAILABLE/NONE record — carry no `draft` key at all. Those must keep the
    non-draft narrative rather than acquiring draft advice from a missing field;
    `is True` (not truthiness) is what guarantees it."""
    d = _describe(monkeypatch, None)
    assert NONDRAFT_REMEDY_ANCHOR in d
    assert "DRAFT" not in d


# ── detection is unchanged: a draft is still flagged, still HIGH ────────────

def test_draft_is_still_flagged_high_with_the_same_signal(monkeypatch):
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    cnc._file_investigate(_entry(True))
    body = rt.bodies[0]
    assert body["priority"] == "HIGH"
    assert body["origin_signal"].startswith(cnc.STRANDED_SIGNAL_PREFIX)
    assert "g-335-1142" in body["title"]


# ── the record shape carries the field everywhere ───────────────────────────

def test_unavailable_record_carries_draft_key():
    """classify_stranded fills missing shas with _PR_UNAVAILABLE, so a shape
    that lacks the key would make `.get("draft")` silently ambiguous between
    'not a draft' and 'never probed'."""
    assert "draft" in cnc._PR_UNAVAILABLE
    assert cnc._PR_UNAVAILABLE["draft"] is None


# ── the PRODUCTION shape: `draft` must survive classify_stranded () ─

import datetime  # noqa: E402


def test_draft_survives_the_entry_classify_stranded_builds():
    """EVERY TEST ABOVE HAND-BUILDS THE ENTRY, so they proved the fork's logic
    and never its wiring — and the wiring was broken the whole time.

    classify_stranded rebuilds entry["pull_request"] FIELD BY FIELD from the
    probe record, and `draft` was absent from that list. _file_investigate
    reads pr.get("draft") off THAT dict, so `_is_draft` could only ever be
    False and this entire fork was inert in production from the day it
    shipped. Measured 2026-08-15 (zeta, hostname cc-02, uname -r
    6.8.0-137-generic): the forge returned draft=true for PR #425 while all 8
    live stranded entries read draft=None.

    This is the guard-920 / rb-5235 class — a suite that replicates the
    contract-ideal argument shape instead of the production one. The fix is
    one field; the test that catches it has to drive the real path.
    """
    now = datetime.datetime(2026, 8, 15, 12, 0, 0)
    sha = "7896aa5470fd203e07798ac373b45448063ee75d"
    goal = {
        "id": "g-115-6217", "_source": "world", "_aspiration_id": "asp-115",
        "status": "completed", "completed_at": "2026-08-14T06:00:00",
        "title": "self-serve API key revoke and rotate",
        "verification": {"outcomes": [f"commit {sha}"]},
    }
    entry = cnc.classify_stranded(
        goal, now,
        sha_status={sha: True},        # on a remote branch
        default_status={sha: False},   # but not the default branch
        pr_status={sha: {"state": "OPEN", "number": 425,
                         "url": "https://example.invalid/pull/425",
                         "title": "api key controls",
                         "created_at": "2026-08-13T06:00:00",
                         "merge_commit_sha": None,
                         "draft": True}})
    assert entry is not None and entry["reason"] == "stranded_open_pr"
    assert entry["pull_request"]["draft"] is True, (
        "classify_stranded dropped `draft`, so the draft fork cannot fire")


def test_draft_from_classify_stranded_reaches_the_draft_remedy(monkeypatch):
    """End-to-end on the production path: probe record -> classify_stranded ->
    _file_investigate. This is the assertion that would have gone red for the
    whole period the fork was inert."""
    now = datetime.datetime(2026, 8, 15, 12, 0, 0)
    sha = "7896aa5470fd203e07798ac373b45448063ee75d"
    goal = {
        "id": "g-115-6217", "_source": "world", "_aspiration_id": "asp-115",
        "status": "completed", "completed_at": "2026-08-14T06:00:00",
        "title": "self-serve API key revoke and rotate",
        "verification": {"outcomes": [f"commit {sha}"]},
    }
    entry = cnc.classify_stranded(
        goal, now, sha_status={sha: True}, default_status={sha: False},
        pr_status={sha: {"state": "OPEN", "number": 425,
                         "url": "https://example.invalid/pull/425",
                         "title": "api key controls",
                         "created_at": "2026-08-13T06:00:00",
                         "merge_commit_sha": None, "draft": True}})
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    cnc._file_investigate(entry)
    d = rt.bodies[0]["description"]
    assert "do NOT merge it on the strength of this goal" in d
    assert "OPEN as a DRAFT after" in d
