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


def test_artifact_draft_is_told_to_discharge_it(monkeypatch):
    d = _describe(monkeypatch, "Caps retention on a write-only archive. Green.")
    assert "HANDOFF ARTIFACT" in d
    assert "mark the pull request ready and merge it" in d
    assert "never a squash" in d, "guard-3465 must ride along with any merge advice"


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
