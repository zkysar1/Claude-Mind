"""test_capability_gate_imperative_noun.py — regression test for .

Bug 2 of g-115-2814: the capability-gate DEFER path over-matched common English
words in defer_reason text, refusing legitimate defers AND auto-filing spurious
Unblock goals (the Layer-D auto-conversion in aspirations.py cmd_update_goal).
Live repro (2026-07-22, zeta) reproduced FIVE over-matches across two classes:

  CLASS A — an imperative verb used as a NOUN (4/5). audit/commit/scan/review
  are in _IMPERATIVE_VERBS, so they bypass the g-248-105 single-token
  distinctiveness rule; used as nouns they matched unrelated capability skills:
    - "a manual audit by the ... team"  -> audit   -> audit-roblox-deliverable
    - "a full security scan of the ..." -> scan    -> access-email
    - "needs review from the ... owner" -> review  -> fresh-eyes-code
    - "a commit to one approach"        -> commit  -> ship-product-pr
  CLASS B — incidental multi-token / compound-token prose (1/5). "the analysis
  is complete but human sign-off on the conclusions is pending" (genuinely
  user-only) matched analyze-npc-behavior on {analysis, human} (multi-token,
  always survives) AND audit-roblox-deliverable on {sign-off} (compound token,
  passes the single-token rule).

Fix (g-115-2829, rb-2996, guard-958 — surgical, NOT a blunt bare-imperative
suppression):
  - _IMPERATIVE_NOUN_GOVERNOR_PRE + _NOUN_USE_PREP_AFTER: a Class-A
    context-disqualifier. An imperative verb GOVERNED by an article/need-verb
    immediately before AND FOLLOWED by an UNAMBIGUOUS external/source/object
    preposition (by | of | from) reads as a noun phrase whose complement is an
    external agent/source/object, not an action request. The 'to'/target
    prepositions are DELIBERATELY EXCLUDED (fresh-eyes review 2026-07-22): 'to'
    ambiguously marks an agent-action target ("commit to main"), so per guard-958
    (fail toward matching) it stays matchable — fixing 3 of the 4 Class-A FPs and
    leaving the ambiguous "a commit to one approach" safely blocking (see
    test_ambiguous_to_commit_left_blocking). False-negative-safe by construction:
    a genuine verb request is followed by an OBJECT (determiner+noun) not a bare
    by/of/from, and a verb+preposition WITHOUT a governing article stays matched.
  - _STOPWORDS += human, sign-off: Class-B pure user-approval prose tokens,
    never a capability identifier (g-115-2336 precedent) — fixes the 4th FP.

guard-958 recipe honored: (a) surgical disqualifier scoped to the reproduced
case; (b) the ADVERSARIAL single-keyword recall control below proves every
genuine agent-provisionable action STILL blocks (inflected/verb+object/
verb+prep-no-article forms), so the gate is not weakened in the g-115-792
wrongly-user-gated direction.

Subprocess + sys.path import shape matches test_capability_gate_prose_collision.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"


def _run_gate(failure_reason: str,
              intended_participants: str = "user") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed)."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", intended_participants,
        "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


# ── The reproduced FPs fixed by this change — each is a genuinely user-only /
#    external defer that must NOT block after the fix (id, defer_reason). Uses the
#    unambiguous external/source/object prepositions by | of | from, or the
#    Class-B stopword demotion. NOTE: the 5th reproduced FP ("a commit to one
#    approach") is DELIBERATELY LEFT BLOCKING (see
#    test_ambiguous_to_commit_left_blocking) — 'to' is an ambiguous
#    action-target preposition, so per guard-958 (fail toward matching) it stays
#    matchable to protect the "a commit to main" recall path. ──
_FP_CASES = [
    ("manual_audit",
     "Waiting for a manual audit by the external security team."),
    ("scan_noun",
     "A full security scan of the vendor system is scheduled for next quarter."),
    ("owner_review",
     "The migration plan needs review from the architecture owner before we "
     "proceed."),
    ("human_signoff",
     "The analysis is complete but human sign-off on the conclusions is pending."),
]

# ── ADVERSARIAL single-keyword recall control (guard-958 mandate b): genuine
#    agent-provisionable actions that MUST STILL block after the fix. The
#    verb+preposition-WITHOUT-article cases are the critical FN-safety proof. ──
_RECALL_CASES = [
    ("commit_object",   "Need to commit the hotfix and push it to production."),
    ("commit_to_main",  "Blocked on commit to main; the working tree has the fix staged."),
    ("push_to_origin",  "Waiting to push to origin after the review passes."),
    ("deploy_object",   "Ready to deploy the env-server build."),
    ("deploy_to_prod",  "Need to deploy to production once tests pass."),
    ("audit_object",    "Should audit the roblox deliverable before shipping."),
    ("review_and_merge","Need to review the migration plan and merge it."),
    ("scan_object",     "Must scan the inbox for the deploy alert."),
    ("merge_to_master", "Blocked on merge to master branch."),
    ("restart_service", "Need to restart the env-server service."),
    ("probe_endpoint",  "Should probe the operator-api health endpoint."),
    ("audit_deliv_signoff",
     "Need to audit the roblox deliverable and sign-off."),
    # Article + git-verb + 'to' + target — the fresh-eyes-review recall-hole
    # cases (2026-07-22). 'to' is EXCLUDED from _NOUN_USE_PREP_AFTER precisely so
    # these keep blocking even WITH a governing article. Guards against a future
    # edit that re-adds 'to' to the prep set (which reopened the  hole).
    ("article_commit_to_main", "A commit to main is needed to unblock the branch."),
    ("article_merge_to_master", "The merge to master is pending and needed."),
]


@pytest.mark.parametrize("case_id,defer_reason", _FP_CASES,
                         ids=[c[0] for c in _FP_CASES])
def test_imperative_noun_defer_does_not_block(case_id, defer_reason):
    """Each reproduced over-match must now PASS (defer permitted, no spurious
    Unblock). would_block=False."""
    _, d = _run_gate(defer_reason)
    assert not d.get("would_block"), (
        f"[{case_id}] over-match not fixed — defer wrongly blocked; "
        f"matches={d.get('matches')} keywords={sorted(d.get('keywords_extracted') or [])}"
    )


@pytest.mark.parametrize("case_id,defer_reason", _RECALL_CASES,
                         ids=[c[0] for c in _RECALL_CASES])
def test_genuine_action_still_blocks(case_id, defer_reason):
    """Adversarial recall (guard-958): a genuine agent-provisionable action is
    still routed away from the user. The verb+preposition-without-article cases
    (commit_to_main, push_to_origin, merge_to_master, deploy_to_prod) are the
    critical false-negative-safety proof — the noun-disqualifier requires a
    GOVERNING ARTICLE, which these lack, so they stay matched."""
    _, d = _run_gate(defer_reason)
    assert d.get("would_block"), (
        f"[{case_id}] RECALL LOSS — genuine action no longer detected "
        f"(g-115-792 wrongly-user-gated risk); "
        f"keywords={sorted(d.get('keywords_extracted') or [])} matches={d.get('matches')}"
    )


def test_human_and_signoff_not_extracted():
    """Class-B: 'human' / 'sign-off' are pure user-approval prose — stopworded
    out of extraction so they never contribute a match (g-115-2336 precedent)."""
    _, d = _run_gate(dict(_FP_CASES)["human_signoff"])  # the human_signoff defer
    kws = set(d.get("keywords_extracted") or [])
    leaked = {"human", "sign-off"} & kws
    assert not leaked, (
        f"user-approval prose leaked into extraction: {sorted(leaked)} "
        f"(all extracted: {sorted(kws)})"
    )


def test_verb_object_form_unaffected():
    """The disqualifier is scoped to noun-use (article-before + preposition-
    after). A verb+object request ("commit the change to origin") must be
    unaffected — 'commit' is followed by an OBJECT, not a bare preposition."""
    _, d = _run_gate("A quick fix is needed; commit the change to origin.")
    assert d.get("would_block"), (
        f"verb+object form wrongly disqualified (over-disqualification); "
        f"keywords={sorted(d.get('keywords_extracted') or [])} matches={d.get('matches')}"
    )


def test_ambiguous_to_commit_left_blocking():
    """DELIBERATE RESIDUAL (not a missed fix): the 5th reproduced FP "a commit to
    one approach" (a design decision) is LEFT BLOCKING. 'to' is an ambiguous
    action-target preposition ("a commit to main" IS an agent action), so per
    guard-958 (fail toward matching when ambiguous) 'to' is excluded from
    _NOUN_USE_PREP_AFTER — trading this narrow semi-FP for recall safety on the
    "a commit to main"/"the merge to master" cases the fresh-eyes review flagged.
    If a future edit makes this PASS by adding 'to' to the prep set, the two
    article_*_to_* recall guards above will fail — that is the intended tripwire."""
    _, d = _run_gate(
        "The team has not committed to the design direction; a commit to one "
        "approach is needed first."
    )
    assert d.get("would_block"), (
        "ambiguous 'a commit to one approach' should stay BLOCKING (guard-958 "
        "fail-toward-matching); if this now passes, confirm 'to' was not re-added "
        f"to _NOUN_USE_PREP_AFTER. matches={d.get('matches')}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
