"""Tests for adjudication-lane.py catch_rate splitting by challenge kind ().

THE DEFECT. The lane published ONE catch_rate that summed two findings with
OPPOSITE consequences. A `wrong` challenge says an ENTRY is defective — evidence
FOR adopting a standing review lane. An `under-cross-linked` challenge says the
entry is correct but its mechanism-axis neighbours are uncited — a corpus-wide
citation-hygiene gap that ONE SWEEP fixes, i.e. evidence AGAINST. Summed, a corpus
defect reads as an entry-quality problem, and the pilot's adopt/drop decision
(g-306-401) is made on exactly that number.

MEASURED (bravo, cc-05, 2026-09-07, n=5 stratified across all four non-self
authors): every sampled entry drew the SAME challenge and no other. A reviewer
finding five DIFFERENT problems is measuring entries; one finding the SAME problem
five times is measuring how entries get written. The uniformity is the evidence.

WHY `unclassified` IS A STATE AND NOT A KIND, pinned in both directions below: all
53 review rows in the live ledger when this shipped predate the field. A split that
silently dropped them would publish a confident rate over a tiny classified tail —
the same mislabelling this split exists to prevent, one level down. So the
unclassified count and `classified_share` travel WITH the split (guard-4859: the
denominator rides with the share), and the kind is never inferred from `basis`.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "adjudication_lane",
    Path(__file__).resolve().parents[1] / "adjudication-lane.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

WRONG, UNDER = "wrong", "under-cross-linked"


def _rows(challenges, agrees=0, unclassified=0):
    """Build review rows from {kind: n} plus agree/unclassified counts.

    Stances come from the module's own vocabulary rather than literals, so a
    change there fails here instead of silently making every challenge read zero
    (guard-1220: read the expected value from the other component).
    """
    challenge = mod.CHALLENGE_STANCES[0]
    agree = next(s for s in mod.STANCES if s not in mod.CHALLENGE_STANCES)
    out = []
    for kind, n in challenges.items():
        # A COMPREHENSION, never `[{...}] * n` -- the multiply form yields n
        # REFERENCES to one dict, so a caller that later stamps a per-row field
        # (as the independence test does with reviewed_by) rewrites every row at
        # once. That produced a 1.0 concentration on a deliberately balanced
        # corpus and failed the test on its first run.
        out += [{"kind": "review", "stance": challenge, "challenge_kind": kind} for _ in range(n)]
    # A challenge row written before the field existed: no key at all.
    out += [{"kind": "review", "stance": challenge} for _ in range(unclassified)]
    out += [{"kind": "review", "stance": agree} for _ in range(agrees)]
    return out


# --- the split itself, both directions --------------------------------------

def test_the_two_kinds_are_counted_separately_not_summed():
    """The regression. 8 challenges that a single catch_rate would report as one
    number must come back as 3 wrong and 5 under-cross-linked."""
    out = mod.catch_rate_by_challenge_kind(_rows({WRONG: 3, UNDER: 5}, agrees=2))
    assert out["by_kind"][WRONG]["count"] == 3
    assert out["by_kind"][UNDER]["count"] == 5
    assert out["challenges"] == 8
    assert out["total_reviews"] == 10


def test_a_single_kind_corpus_does_not_report_the_other():
    """NEGATIVE CONTROL. Without it the test above passes against a splitter that
    always emits both keys, and the discrimination would be decorative."""
    out = mod.catch_rate_by_challenge_kind(_rows({UNDER: 6}, agrees=4))
    assert out["by_kind"][UNDER]["count"] == 6
    assert WRONG not in out["by_kind"]


def test_the_measured_shape_reads_as_a_corpus_defect_not_an_entry_defect():
    """bravo's n=5: every challenge the same kind. The split must make that
    legible as 100% one kind, which is the whole adopt/drop argument."""
    out = mod.catch_rate_by_challenge_kind(_rows({UNDER: 5}))
    assert out["by_kind"][UNDER]["share_of_classified_challenges"] == 1.0
    assert out["classified_share"] == 1.0


# --- unclassified is a STATE, never a guess ---------------------------------

def test_rows_predating_the_field_are_unclassified_not_attributed():
    """The live corpus at ship time. A row with no challenge_kind key must land in
    `unclassified` and must NOT be attributed to either kind."""
    out = mod.catch_rate_by_challenge_kind(_rows({}, unclassified=53))
    assert out["unclassified"] == 53
    assert out["classified"] == 0
    assert out["by_kind"] == {}
    assert out["classified_share"] == 0.0


def test_classified_share_exposes_how_much_of_the_corpus_the_split_speaks_for():
    """The guard-4859 half: a reader must be able to see that a clean-looking
    by_kind covers only a fraction of the challenges."""
    out = mod.catch_rate_by_challenge_kind(_rows({WRONG: 2}, unclassified=8))
    assert out["challenges"] == 10
    assert out["classified"] == 2
    assert out["classified_share"] == 0.2


def test_an_unknown_kind_value_is_treated_as_unclassified_not_as_a_new_kind():
    """A row carrying a kind outside the vocabulary must not silently create a
    bucket — that would let a typo read as a finding."""
    rows = [{"kind": "review", "stance": mod.CHALLENGE_STANCES[0], "challenge_kind": "typo-kind"}]
    out = mod.catch_rate_by_challenge_kind(rows)
    assert out["unclassified"] == 1
    assert out["by_kind"] == {}


# --- denominators, which is where a split like this usually goes wrong -------

def test_rate_of_all_reviews_shares_the_headline_denominator():
    """rate_of_all_reviews must be over TOTAL reviews so it composes with the
    headline catch_rate the split is splitting."""
    out = mod.catch_rate_by_challenge_kind(_rows({WRONG: 2, UNDER: 2}, agrees=6))
    assert out["total_reviews"] == 10
    assert out["by_kind"][WRONG]["rate_of_all_reviews"] == 0.2
    assert out["by_kind"][UNDER]["rate_of_all_reviews"] == 0.2


def test_share_of_classified_excludes_unclassified_from_its_denominator():
    """The deliberate choice, pinned so it is not 'simplified' later: including
    unclassified rows would drag every share toward zero and read as though those
    kinds were rarer than measured."""
    out = mod.catch_rate_by_challenge_kind(_rows({WRONG: 1, UNDER: 1}, unclassified=8))
    assert out["by_kind"][WRONG]["share_of_classified_challenges"] == 0.5
    assert out["by_kind"][UNDER]["share_of_classified_challenges"] == 0.5


def test_agree_rows_are_not_challenges():
    """An AGREE row has nothing to classify and must not inflate any denominator
    except total_reviews."""
    out = mod.catch_rate_by_challenge_kind(_rows({}, agrees=7))
    assert out["total_reviews"] == 7
    assert out["challenges"] == 0
    assert out["classified_share"] is None


def test_empty_corpus_does_not_raise_and_claims_nothing():
    out = mod.catch_rate_by_challenge_kind([])
    assert out["total_reviews"] == 0
    assert out["challenges"] == 0
    assert out["by_kind"] == {}
    assert out["classified_share"] is None


# --- the three guards are independent axes ----------------------------------

def test_kind_split_is_independent_of_sample_size_and_of_concentration():
    """The premise the three guards rest on: a corpus can clear MIN_REPORTABLE_N,
    be perfectly balanced across reviewers, and still be entirely unclassified —
    so no one guard substitutes for another."""
    n = mod.MIN_REPORTABLE_N * 2
    rows = _rows({}, unclassified=n)
    for i, r in enumerate(rows):
        r["reviewed_by"] = ["alpha", "bravo", "echo"][i % 3]
    kind = mod.catch_rate_by_challenge_kind(rows)
    conc = mod.reviewer_concentration(rows)
    assert len(rows) >= mod.MIN_REPORTABLE_N      # denominator guard satisfied
    assert conc["concentrated"] is False          # scope guard satisfied
    assert kind["classified"] == 0                # meaning guard NOT satisfied
