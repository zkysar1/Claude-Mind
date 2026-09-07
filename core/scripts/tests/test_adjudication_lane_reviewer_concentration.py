"""Tests for adjudication-lane.py reviewer-concentration scoping ().

THE DEFECT. `report` guarded exactly one property of its headline catch-rate:
`n >= MIN_REPORTABLE_N`. That guards the DENOMINATOR. Nothing guarded the SCOPE
— whose rows fill it — so a corpus written almost entirely by one reviewer
produced a precise, well-formed, fleet-LABELLED ratio that generalised no
further than that one reviewer. The pilot's terminal act is to encode an
adopt/drop decision from exactly this number.

MEASURED on the live ledger 2026-09-07 (alpha, cc-10): n=46 cleared the n>=20
gate comfortably while `by_reviewer` read {alpha: 42, echo: 3, bravo: 1} — 91.3%
one reviewer. Three consecutive review passes flagged the concentration in prose
and none could make the instrument say it, because the instrument had no field
for it. That is why the fix is in the report and not in another note.

WHY QUALIFY RATHER THAN REFUSE, pinned below in both directions: a thin n means
the ratio is noise, so refusing loses nothing. A concentrated n means the ratio
is correct and mislabelled — refusing would destroy a figure the window-close
reader needs. guard-2193's remedy for a fleet-scoped condition read through a
single-vantage instrument is to report the distribution or say plainly that the
reading is one vantage; it is not to withhold the reading.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "adjudication_lane",
    Path(__file__).resolve().parents[1] / "adjudication-lane.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _reviews(spec):
    """Build review rows from {reviewer: (n_challenge, n_agree)}.

    Stances come from the module's own CHALLENGE_STANCES rather than a literal,
    so a change to that vocabulary fails here instead of silently making every
    challenge count read zero (guard-1220: read the expected value from the
    other component, not from a restatement).
    """
    rows = []
    challenge = mod.CHALLENGE_STANCES[0]
    agree = next(s for s in mod.STANCES if s not in mod.CHALLENGE_STANCES)
    for who, (n_chal, n_agree) in spec.items():
        rows += [{"kind": "review", "reviewed_by": who, "stance": challenge}] * n_chal
        rows += [{"kind": "review", "reviewed_by": who, "stance": agree}] * n_agree
    return rows


# --- the concentration verdict, both directions -----------------------------

def test_one_dominant_reviewer_is_flagged_concentrated():
    """The regression itself, at the live shape: 42/46 rows from one reviewer."""
    out = mod.reviewer_concentration(_reviews({"alpha": (28, 14), "echo": (2, 1), "bravo": (1, 0)}))
    assert out["concentrated"] is True
    assert out["dominant_reviewer"] == "alpha"
    assert out["dominant_share"] > mod.MAX_REVIEWER_SHARE
    assert out["distinct_reviewers"] == 3


def test_balanced_corpus_is_not_flagged():
    """NEGATIVE CONTROL. Without this the test above passes on a predicate that
    is simply always True, which is how a scope guard becomes decorative."""
    out = mod.reviewer_concentration(_reviews({"alpha": (5, 5), "bravo": (5, 5), "echo": (5, 5)}))
    assert out["concentrated"] is False
    assert out["dominant_share"] <= mod.MAX_REVIEWER_SHARE
    assert out["distinct_reviewers"] == 3


def test_sole_reviewer_is_fully_concentrated():
    """The extreme the pilot started from: share 1.0, one distinct reviewer."""
    out = mod.reviewer_concentration(_reviews({"alpha": (10, 10)}))
    assert out["concentrated"] is True
    assert out["dominant_share"] == 1.0
    assert out["distinct_reviewers"] == 1


def test_empty_corpus_does_not_raise_and_does_not_claim_concentration():
    """A zero-row ledger must not divide by zero, and absence of rows is not
    evidence of concentration — `concentrated` stays False with share None."""
    out = mod.reviewer_concentration([])
    assert out["dominant_share"] is None
    assert out["concentrated"] is False
    assert out["dominant_reviewer"] is None
    assert out["per_reviewer"] == {}


# --- the per-reviewer split (the discriminator the pilot needs) -------------

def test_per_reviewer_carries_each_reviewers_own_catch_rate():
    """A lane-level mean cannot express reviewer AGREEMENT, which is what
    separates 'these entries need challenging' from 'this reviewer challenges
    things'. Two reviewers with opposite rates must remain distinguishable."""
    out = mod.reviewer_concentration(_reviews({"alpha": (8, 2), "bravo": (1, 9)}))
    assert out["per_reviewer"]["alpha"] == {"reviewed": 10, "challenged": 8, "catch_rate": 0.8}
    assert out["per_reviewer"]["bravo"] == {"reviewed": 10, "challenged": 1, "catch_rate": 0.1}


def test_a_reviewer_who_never_challenges_reports_zero_not_missing():
    """An all-AGREE reviewer must appear with catch_rate 0.0. Dropping the key
    would make the split silently agree with the dominant reviewer."""
    out = mod.reviewer_concentration(_reviews({"alpha": (5, 5), "bravo": (0, 5)}))
    assert out["per_reviewer"]["bravo"]["catch_rate"] == 0.0
    assert out["per_reviewer"]["bravo"]["reviewed"] == 5


# --- the two guards are independent axes ------------------------------------

def test_concentration_is_independent_of_sample_size():
    """The whole premise: a corpus can clear MIN_REPORTABLE_N and still be
    single-vantage. Sized from the module's own constant so it cannot drift."""
    n = mod.MIN_REPORTABLE_N * 3
    out = mod.reviewer_concentration(_reviews({"alpha": (n // 2, n - n // 2), "bravo": (1, 0)}))
    assert out["per_reviewer"]["alpha"]["reviewed"] >= mod.MIN_REPORTABLE_N
    assert out["concentrated"] is True


def test_a_thin_but_balanced_corpus_is_not_concentrated():
    """The other corner, and the reason the two guards are not one guard: below
    MIN_REPORTABLE_N the rate is refused for noise, but nothing here is
    out-of-scope, so this axis must stay quiet."""
    out = mod.reviewer_concentration(_reviews({"alpha": (1, 1), "bravo": (1, 1), "echo": (1, 1)}))
    assert out["concentrated"] is False
    assert sum(c["reviewed"] for c in out["per_reviewer"].values()) < mod.MIN_REPORTABLE_N
