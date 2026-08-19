"""A goal REDONE on a second branch must not be told to merge its first attempt
(g-115-6295).

completed-not-committed-sweep tier 2 observes "commits naming goal X sit on an
unmerged branch" and concluded "goal X's deliverable has not reached the default
branch, so no user can see it. Merge the pull request." The premise does not
entail the conclusion: a goal redone on a second branch satisfies the first and
not the second — the abandoned first attempt's commits are genuinely stranded
while the deliverable shipped from elsewhere.

Measured base rate over the five-goal cluster this fix was filed from: 4 of 5
remedies falsified, against a diagnosis that was correct every single time. Two
of the four were WASTE (the remedy pointed at work already landed); one was HARM
(following it merged a structurally undeployable PR and broke a live place).

The fixtures below are the REAL commit subjects measured on this box
(hostname cc-02, uname -r 6.8.0-137-generic) on 2026-08-15 — not invented ones —
because the two false-positive classes they encode are precisely what a
plausible-looking implementation gets wrong.

What must NOT change is the DETECTION. A goal that closed `completed` while its
first attempt sits unlanded on an open PR is a genuine finding whether or not a
second attempt shipped. Only the remedy forks — these tests pin both halves.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_landed", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

SHA = "54af9e0c3f380fbf5caf47bca24dc0029995cce8"
PRODUCT_REPO = "/repos/Ayoai-Operator"
MIND_REPO = "/repos/ayoai-mind"

# Measured 2026-08-15. The deliverable that really did land under a second
# commit — the case the fork exists to catch.
REDONE = "fix(g-335-1212): key the absence and startup grace on the claim anchor"

# Measured 2026-08-15. The Mind repo commits its own goal-queue writes with the
# FLAGGED goal's id in the subject BODY, so a free-text grep matches the very
# commit that filed the Investigate — making the sweep suppress its own correct
# findings, and more likely to as an Investigate ages.
AUDIT_TRAIL = ("docs(g-115-6217): g-335-955 closed completed but its commit is "
               "stranded on an unmerged branch (PR #392)")

# Measured 2026-08-15. A revert names the goal id via the merged PR's branch and
# means the OPPOSITE of landed. Free-text scored 5 hits on this goal; scope
# anchoring scores 0.
REVERT = ('Revert "Merge pull request #176 from '
          'zkysar1/fix/g-250-362-obstacle-avoidance"')


def _log(*subjects):
    return "\n".join(f"abc123{i}\x1f{s}" for i, s in enumerate(subjects))


def _fake_git(holding_repo, log_by_repo, log_rc=0):
    def _git(repo, *args, timeout=15):
        if args[:2] == ("cat-file", "-e"):
            return (0, "") if str(repo) == holding_repo else (1, "")
        if args and args[0] == "log":
            return (log_rc, log_by_repo.get(str(repo), "") if log_rc == 0 else "")
        return (1, "")
    return _git


def _probe(monkeypatch, holding_repo, log_by_repo, goal_id="g-335-1212",
           repos=(PRODUCT_REPO, MIND_REPO), refs=None, log_rc=0):
    monkeypatch.setattr(cnc, "_git", _fake_git(holding_repo, log_by_repo, log_rc))
    if refs is None:
        refs = {PRODUCT_REPO: "origin/main", MIND_REPO: "origin/main"}
    return cnc.probe_goalid_scoped_on_default(goal_id, [SHA], list(repos), refs)


# ── discriminator 1: conventional-commit SCOPE, not free text ───────────────

def test_scope_anchor_accepts_the_conventional_forms():
    pat = cnc._scope_re("g-335-1212")
    assert pat.match("fix(g-335-1212): a subject")
    assert pat.match("feat(g-335-1212)!: a breaking subject")
    assert pat.match("chore(g-335-1212): another")


def test_scope_anchor_rejects_a_revert_naming_the_goal():
    """Measured harm case: following the merge remedy here broke a live place.
    A revert names the goal id and means the opposite of landed."""
    assert cnc._scope_re("g-250-362").match(REVERT) is None


def test_scope_anchor_rejects_a_body_mention():
    """The sweep's own Investigate-filing commit carries the FLAGGED goal's id
    in its subject body. Matching it would make the sweep suppress itself."""
    assert cnc._scope_re("g-335-955").match(AUDIT_TRAIL) is None


def test_scope_anchor_still_matches_that_commit_for_its_OWN_scope():
    """The positive control for the test above: the same subject IS a genuine
    scope match for g-115-6217. So discriminator 1 alone cannot separate them —
    which is exactly why discriminator 2 exists."""
    assert cnc._scope_re("g-115-6217").match(AUDIT_TRAIL) is not None


def test_probe_drops_a_revert_and_a_body_mention(monkeypatch):
    hits = _probe(monkeypatch, PRODUCT_REPO,
                  {PRODUCT_REPO: _log(REVERT, AUDIT_TRAIL)},
                  goal_id="g-250-362")
    assert hits == []


def test_probe_returns_the_redone_commit(monkeypatch):
    hits = _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: _log(REDONE)})
    assert len(hits) == 1
    assert "g-335-1212" in hits[0]


# ── discriminator 2: SAME REPO as the stranded commits ──────────────────────

def test_a_hit_in_another_repo_does_not_count(monkeypatch):
    """Measured on : it landed its docs half in the Mind repo while
    its UI half stayed stranded on a DRAFT PR in a product repo. Counting the
    foreign hit would have told an agent to close a draft whose own body reads
    'not shippable alone'."""
    hits = _probe(monkeypatch, PRODUCT_REPO,
                  {MIND_REPO: _log("docs(g-335-1212): the docs half")})
    assert hits == []


def test_the_same_log_counts_when_it_is_the_holding_repo(monkeypatch):
    """Positive control for the test above — identical log, holding repo moved,
    so the empty result there is attributable to repo scoping and nothing else."""
    hits = _probe(monkeypatch, MIND_REPO,
                  {MIND_REPO: _log("docs(g-335-1212): the docs half")})
    assert len(hits) == 1


# ── conservative in the keep-the-flag direction ─────────────────────────────

def test_unlocatable_repo_is_undeterminable(monkeypatch):
    assert _probe(monkeypatch, "/repos/nowhere",
                  {PRODUCT_REPO: _log(REDONE)}) is None


def test_unknown_default_ref_is_undeterminable(monkeypatch):
    assert _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: _log(REDONE)},
                  refs={PRODUCT_REPO: None, MIND_REPO: None}) is None


def test_git_error_is_undeterminable(monkeypatch):
    """A probe error must never be read as 'nothing landed' — that direction
    forks the remedy on an absence of evidence, which is this sweep's own
    defect committed by its own fix."""
    assert _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: _log(REDONE)},
                  log_rc=1) is None


def test_probed_and_empty_is_distinguishable_from_undeterminable(monkeypatch):
    """The whole point of the tri-state: [] and None are both falsy and both
    keep the flag, but only one of them is a FINDING (guard-1641)."""
    determined = _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: ""})
    assert determined == [] and determined is not None


# ── apply_landed_elsewhere is pure and is NOT a suppressor ──────────────────

def _entry(**over):
    e = {"goal_id": "g-335-1212", "source": "world", "title": "t",
         "age_hours": 30.0, "reason": "stranded_open_pr",
         "shas_off_default": [SHA],
         "pull_request": {"number": 201, "url": "https://example.invalid/pull/201",
                          "age_hours": 30.0, "state": "OPEN", "draft": False}}
    e.update(over)
    return e


def test_apply_does_not_change_reason_or_suppress():
    e = cnc.apply_landed_elsewhere(_entry(), {"g-335-1212": ["abc1230 " + REDONE]})
    assert e["reason"] == "stranded_open_pr"
    assert e.get("benign_superseded") is None
    assert e["landed_elsewhere"] == ["abc1230 " + REDONE]
    assert e["landed_elsewhere_probed"] is True


def test_apply_records_probed_on_a_determined_empty():
    e = cnc.apply_landed_elsewhere(_entry(), {"g-335-1212": []})
    assert e["landed_elsewhere"] == []
    assert e["landed_elsewhere_probed"] is True


def test_apply_defaults_to_not_landed_and_not_probed():
    """A goal absent from the status map was never probed. Both fields must
    say so: falsy hits (fail-safe to the not-landed branch) AND probed=False
    (so the remedy does not claim a finding nobody made)."""
    e = cnc.apply_landed_elsewhere(_entry(), {})
    assert e["landed_elsewhere"] == []
    assert e["landed_elsewhere_probed"] is False


# ── the remedy fork ─────────────────────────────────────────────────────────

class _CapturingRT:
    def __init__(self):
        self.bodies = []

    def aspirations_add_goal(self, asp_id, body, source=None):
        self.bodies.append(body)
        return {"id": "g-115-9999"}


def _describe(monkeypatch, entry):
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    cnc._file_investigate(entry)
    assert len(rt.bodies) == 1
    return rt.bodies[0]["description"]


LANDED = ["0f39d04 " + REDONE]


def test_landed_elsewhere_forks_to_superseded(monkeypatch):
    d = _describe(monkeypatch, _entry(landed_elsewhere=LANDED))
    assert "DO NOT MERGE" in d
    assert "CLOSE the pull request as superseded" in d
    assert "0f39d04" in d


def test_landed_elsewhere_outranks_the_draft_fork(monkeypatch):
    """When the deliverable already shipped, 'close it as superseded' is right
    whether or not the stranded PR is a draft."""
    e = _entry(landed_elsewhere=LANDED)
    e["pull_request"]["draft"] = True
    d = _describe(monkeypatch, e)
    assert "DO NOT MERGE" in d
    assert "do NOT merge it on the strength of this goal" not in d


def test_absent_key_takes_the_not_landed_branch(monkeypatch):
    """FAIL-SAFE. Entries composed before this change carry no
    `landed_elsewhere` key; a missing field must never read as 'landed'."""
    e = _entry()
    e.pop("landed_elsewhere", None)
    d = _describe(monkeypatch, e)
    assert "DO NOT MERGE" not in d
    assert "verify the pull request is DEPLOYABLE" in d


def test_probed_empty_claims_the_finding(monkeypatch):
    d = _describe(monkeypatch, _entry(landed_elsewhere=[],
                                      landed_elsewhere_probed=True))
    assert "No default-branch commit carries this goal's scope" in d
    assert "UNKNOWN" not in d


def test_unprobed_does_not_claim_the_finding(monkeypatch):
    """The distinction this whole tri-state exists for: an unrunnable probe
    must not be narrated as 'nothing landed'. That would be exactly the
    reasoning — a conclusion drawn from an absence of evidence — that this
    goal was filed to remove from tier 2."""
    d = _describe(monkeypatch, _entry(landed_elsewhere=[],
                                      landed_elsewhere_probed=False))
    assert "No default-branch commit carries this goal's scope" not in d
    assert "UNKNOWN" in d
    assert "verify the pull request is DEPLOYABLE" in d


def test_absent_probed_key_is_treated_as_unprobed(monkeypatch):
    """Pre-change entries carry neither field. `is True` (not truthiness) is
    what keeps a missing key out of the claims-a-finding branch."""
    e = _entry()
    e.pop("landed_elsewhere_probed", None)
    assert "UNKNOWN" in _describe(monkeypatch, e)


# ── the inference that started this is gone from BOTH branches ──────────────

RETIRED_CONCLUSION = "so no user can see it"


def test_the_deliverable_conclusion_is_never_asserted(monkeypatch):
    """The old text drew a conclusion about the DELIVERABLE from evidence about
    COMMITS, unconditionally — including on the draft branch, where it was
    already known to be unsafe advice. Neither branch may reinstate it."""
    assert RETIRED_CONCLUSION not in _describe(
        monkeypatch, _entry(landed_elsewhere=LANDED))
    assert RETIRED_CONCLUSION not in _describe(
        monkeypatch, _entry(landed_elsewhere=[]))


def test_not_landed_branch_states_the_observation(monkeypatch):
    d = _describe(monkeypatch, _entry(landed_elsewhere=[]))
    assert "Those commits have not reached the default branch." in d


def test_landed_branch_omits_the_not_reached_sentence(monkeypatch):
    d = _describe(monkeypatch, _entry(landed_elsewhere=LANDED))
    assert "have not reached the default branch" not in d


# ── detection unchanged: still filed, still HIGH, same dedup key ────────────

def test_landed_entry_is_still_filed_high_with_the_same_signal(monkeypatch):
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    cnc._file_investigate(_entry(landed_elsewhere=LANDED))
    body = rt.bodies[0]
    assert body["priority"] == "HIGH"
    assert body["origin_signal"].startswith(cnc.STRANDED_SIGNAL_PREFIX)
    assert "g-335-1212" in body["title"]


# ── the window cap is a measurement boundary, not a finding ─────────────────

def test_saturated_window_with_no_match_is_undeterminable(monkeypatch):
    """`git log` returns NEWEST-FIRST, and discriminator 1's own premise is that
    audit-trail commits mentioning a goal accumulate over time. So a genuine
    early `fix(<goal-id>):` can be pushed past the window by later `docs(...)`
    filing commits. A full window with no scope match is a TRUNCATION, and a
    truncation must not be reported as 'nothing landed' (guard-1760)."""
    saturated = _log(*[AUDIT_TRAIL] * 200)
    assert _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: saturated},
                  goal_id="g-335-1212") is None


def test_saturated_window_WITH_a_match_still_reports_it(monkeypatch):
    """A hit is a hit — saturation only matters when the result is empty, so a
    full window that DID find the scope commit is a real measurement."""
    saturated = _log(*([AUDIT_TRAIL] * 199 + [REDONE]))
    hits = _probe(monkeypatch, PRODUCT_REPO, {PRODUCT_REPO: saturated})
    assert hits and len(hits) == 1


def test_unsaturated_empty_window_is_still_a_finding(monkeypatch):
    """The control for the two above: below the cap, empty means measured-empty,
    which the remedy IS allowed to claim."""
    assert _probe(monkeypatch, PRODUCT_REPO,
                  {PRODUCT_REPO: _log(*[AUDIT_TRAIL] * 5)},
                  goal_id="g-335-1212") == []
