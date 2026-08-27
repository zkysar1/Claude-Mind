"""The draft fork's THREE-way classification ().

Before this, every draft PR the sweep flagged got one narrative: "the author
marked it deliberately not-ready." Measured 2026-08-17 against the four PRs the
originating goal named, that sentence was FALSE for three of them — a worker
Body opens a draft as a handoff to the reducer, the reducer closes the goal
`completed`, and nobody owns marking it ready. The sweep had re-derived that
pattern 26 times, once per stranded PR, precisely because both cases produced
identical advice so the classification never happened.

What these tests protect is the ASYMMETRY, not the accuracy. Calling a real hold
an artifact advises shipping a half-feature; calling an artifact a hold leaves it
stranded, which is the status quo. So the discriminator is deliberately generous
about detecting a gate, and an unreadable body must resolve to the conservative
branch rather than to the actionable one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "completed-not-committed-sweep.py"

_spec = importlib.util.spec_from_file_location("cnc_gate", SCRIPT)
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)  # type: ignore[union-attr]


# ── the discriminator ───────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "## Merge gate -- DRAFT on purpose -- do not merge until the DEV deploy lands",
    "Blocked on the backend rollout.",
    "Holding until #204 merges.",
    "This has a precondition: the schema migration must run first.",
    "DO NOT MERGE",                       # case-insensitivity
])
def test_gate_language_is_detected(body):
    assert cnc.pr_declares_merge_gate(body) is True


@pytest.mark.parametrize("body", [
    "Caps PrivateNotes_history retention -- a write-only archive that grew unbounded.",
    "Covers the perception zone WIRING, not just the zone units.\n\nAll green.",
    "Pins the 30-stud range gate the feature PR left untested.",
])
def test_ordinary_bodies_declare_no_gate(body):
    """These three are paraphrases of the real handoff-artifact PRs (#197,
    #201, #205), each of which scored 0/6 on the marker set."""
    assert cnc.pr_declares_merge_gate(body) is False


@pytest.mark.parametrize("body", [None, "", "   ", 123, [], {}])
def test_unavailable_body_is_None_not_False(body):
    """None and False must never be collapsed. False authorizes a merge
    recommendation; None must not. Entries recorded before the body field
    existed all arrive here, so this is the common case on historical data."""
    assert cnc.pr_declares_merge_gate(body) is None


# ── the citation pointer () ───────────────────────────────────────

@pytest.mark.parametrize("body,expected", [
    ("Paired with g-326-116; both halves are needed.", ["g-326-116"]),
    ("closes g-115-7709 and g-335-1009", ["g-115-7709", "g-335-1009"]),
    ("g-115-7709 again, g-115-7709 twice", ["g-115-7709"]),   # de-duplicated
    ("Caps retention on a write-only archive.", []),
    ("", []), (None, []), (123, []),
])
def test_pr_cited_goal_ids(body, expected):
    assert cnc.pr_cited_goal_ids(body) == expected


def test_cited_goal_ids_does_not_require_parentheses():
    """_COMMIT_GOALID_RE parses conventional-commit SUBJECTS and so requires
    parentheses; a PR body cites the id bare. Reusing that pattern here would
    have matched zero real bodies while looking correct."""
    assert cnc._COMMIT_GOALID_RE.findall("Paired with g-326-116") == []
    assert cnc.pr_cited_goal_ids("Paired with g-326-116") == ["g-326-116"]


# ── the field actually reaches the classifier ───────────────────────────────

def test_norm_carries_body_off_the_forge_payload():
    """The  defect, one field over: `draft` was resolved by the
    prober and dropped by the entry rebuild, which made that whole fork inert
    in production while its tests passed. `body` is the only field separating a
    hold from an artifact, so the same omission would silently disable this
    one. Asserted at BOTH hops, because passing either alone proves nothing.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"body": p.get("body")' in src, "probe_sha_pull_request drops body"
    assert '"body": pr.get("body"),' in src, "the entry rebuild drops body"


# ── the three-way fork ──────────────────────────────────────────────────────

class _CapturingRT:
    def __init__(self):
        self.bodies = []

    def aspirations_add_goal(self, asp_id, body, source=None):
        self.bodies.append(body)
        return {"id": "g-115-9999"}


def _describe(monkeypatch, body, draft=True):
    rt = _CapturingRT()
    monkeypatch.setattr(cnc, "_rt", rt)
    pr = {"number": 425, "url": "https://example.invalid/pull/425",
          "age_hours": 39.0, "state": "OPEN", "draft": draft}
    if body is not None:
        pr["body"] = body
    cnc._file_investigate({
        "goal_id": "g-335-1142",
        "source": "world",
        "title": "Add API key rotation and expiry management",
        "age_hours": 38.9,
        "reason": "stranded_open_pr",
        "shas_off_default": ["7896aa5470fd203e07798ac373b45448063ee75d"],
        "pull_request": pr,
    })
    assert len(rt.bodies) == 1
    return rt.bodies[0]["description"]


def test_token_miss_reaches_no_conclusion_and_gives_no_merge_advice(monkeypatch):
    """ / guard-4432. This test previously pinned the OPPOSITE: it
    asserted the 0-of-N branch says "HANDOFF ARTIFACT" and "mark the pull
    request ready and merge it". Both repos in guard-4432's measurement
    auto-deploy on merge to default, so that instruction manufactured authority
    for an unwanted production deploy out of an ABSENCE of matched tokens —
    measured 2-of-2 counterexamples scoring 0-of-6 while carrying gates
    unmistakable to a reader, and 11 of 32 open drafts fleet-wide in the trap.

    The guard-3465 "never a squash" assertion went with it, and deliberately:
    it required squash advice to ride along with MERGE advice, and there is no
    longer any merge advice in this branch for it to ride on.
    """
    d = _describe(monkeypatch, "Caps retention on a write-only archive. Green.")

    # POSITIVE CONTROL — the branch actually RAN. Without these, the absence
    # assertions below would pass just as happily if the fork stopped being
    # reached at all, or emitted an empty remedy (guard-4166).
    assert "NO GATE TEXT MATCHED" in d
    assert "NO EVIDENCE EITHER WAY" in d
    assert "READ the pull request" in d
    assert str(len(cnc._MERGE_GATE_MARKERS)) + " tokens searched" in d, \
        "the remedy must report how many tokens were searched, not just that none hit"

    # The conclusion and the action instruction are both gone.
    assert "HANDOFF ARTIFACT" not in d
    assert "mark the pull request ready and merge it" not in d
    # Anchor on the AFFIRMATIVE instruction, never the bare substring: this
    # branch's own text says "Do NOT mark it ready", which contains it. Same
    # word-collision trap the non-draft test below already documents.
    assert "Do NOT mark it ready" in d
    # The CI claim was never derived from the token scan — nothing here reads
    # CI — so the branch must not assert it either.
    assert "verified and green" not in d


def test_token_miss_names_a_cited_goal_id_when_the_body_has_one(monkeypatch):
    """A token miss routes to a READ; naming the cited goal makes that read
    actionable instead of "go read a wall of text". The citation is used ONLY
    to point the reader — resolving it here would re-derive the same positive
    conclusion guard-4432 forbids, one field over."""
    d = _describe(monkeypatch, "Paired with g-326-116; both halves are needed.")
    assert "g-326-116" in d
    assert "resolve that goal FIRST" in d
    assert "HANDOFF ARTIFACT" not in d


def test_token_miss_without_a_citation_still_reaches_no_conclusion(monkeypatch):
    """The control for the test above: absence of a citation must not tip the
    branch back toward a conclusion."""
    d = _describe(monkeypatch, "Caps retention on a write-only archive.")
    assert "cites no goal id" in d
    assert "not evidence either way" in d
    assert "Do NOT mark it ready" in d


def test_unreadable_body_branch_offers_no_merge_licence(monkeypatch):
    """The None branch carried the SAME defect in its closing clause — "if the
    body turns out to declare NO gate at all, this is a handoff artifact
    instead: mark it ready and merge it". Fixing only the False branch would
    have left the identical unsupported inference reachable one branch over."""
    d = _describe(monkeypatch, None)
    assert "UNKNOWN" in d                      # positive control: branch ran
    assert "handoff artifact instead" not in d
    assert "mark it ready and merge it" not in d
    assert "NOT a licence to merge" in d


def test_gated_draft_keeps_the_do_not_merge_narrative(monkeypatch):
    d = _describe(monkeypatch, "## Merge gate -- do not merge until DEV deploys")
    assert "do NOT merge it on the strength of this goal" in d
    assert "HANDOFF ARTIFACT" not in d
    assert "mark the pull request ready and merge it" not in d


def test_unreadable_body_falls_back_to_the_conservative_branch(monkeypatch):
    """The fail-safe direction. An absent body must not be read as "no gate" —
    that is the one error that ships a half-feature."""
    d = _describe(monkeypatch, None)
    assert "UNKNOWN" in d
    assert "do NOT merge it on the strength of this goal" in d
    assert "HANDOFF ARTIFACT" not in d


def test_the_three_branches_are_mutually_distinguishable(monkeypatch):
    """A fork whose branches emit interchangeable prose is not a fork. Each
    remedy must be identifiable from the filed text alone — that
    indistinguishability is what let this pattern be re-derived 26 times."""
    artifact = _describe(monkeypatch, "Ordinary description, all checks green.")
    gated = _describe(monkeypatch, "Do not merge until the migration lands.")
    unknown = _describe(monkeypatch, None)
    assert artifact != gated != unknown and artifact != unknown


def test_a_non_draft_pr_is_untouched_by_this_fork(monkeypatch):
    """Scope control: the gate discriminator must not reach non-draft PRs,
    whose remedy is decided by other logic entirely."""
    d = _describe(monkeypatch, "Do not merge until the migration lands.",
                  draft=False)
    assert "HANDOFF ARTIFACT" not in d
    # Anchor on a phrase unique to THIS fork's unknown branch, not on the bare
    # word "UNKNOWN": the pre-existing non-draft remedy already says "branch is
    # UNKNOWN" about something else entirely, so the loose form failed on a word
    # collision and would have reported a leak that does not exist.
    assert "whether the author declared a merge gate is" not in d
